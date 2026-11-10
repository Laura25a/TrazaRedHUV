// ==========================================================================
// TrazaRed HUV — "Traza", la asistente flotante (esquina inferior derecha)
// ==========================================================================
// Está en todas las pantallas. Se le escribe o se le HABLA:
//   · "¿Dónde está la paciente Marleny Balanta?"  → busca y te lleva a su ficha
//   · "Busca la cédula 1146215249"               → igual, por documento
//   · "Abre la remisión 45"                       → abre ese formato
//   · "Llévame a instituciones" / "ir a reportes" → navega por el menú
//   · "¿Cuántas remisiones pendientes hay?"       → cuenta y lista para abrir
//   · Cualquier otra pregunta clínica             → la responde el agente
//     clínico (POST /agente/chat) con el paciente que esté en contexto.
// Búsqueda, navegación y conteos se resuelven AQUÍ, con la API del sistema:
// los nombres y documentos no van a ningún modelo de lenguaje. Al agente solo
// le llega el número del paciente; el servidor lo convierte en seudónimo.
//
// Voz: dictado del navegador (Chrome/Edge) y lectura en voz alta
// (speechSynthesis). En "conversación" Traza vuelve a escuchar después de
// responder, hasta que digas "gracias", "listo" o toques el micrófono.
"use strict";

const TRAZA = {
  abierta: false, montada: false, ocupada: false,
  paciente: null,            // {id, nombre} del último paciente encontrado (contexto)
  conversacion: "traza-" + Date.now().toString(36),
  hablar: true,              // leer en voz alta las respuestas
  conversando: false,        // modo manos libres (tras usar el micrófono)
  sr: null, escuchando: false,
};
const TRAZA_SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const TRAZA_CLINICOS = ["admin", "medico", "especialista"];

// ---------- Avatar (dibujo propio, SVG) ----------
const trazaAvatar = (id) => `<svg viewBox="0 0 120 120" class="traza-cara" aria-hidden="true">
  <defs><linearGradient id="${id}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#7fd3e6"/><stop offset=".55" stop-color="#2b8fc9"/><stop offset="1" stop-color="#1d5fbf"/></linearGradient></defs>
  <circle cx="60" cy="60" r="56" fill="url(#${id})" stroke="#0f2744" stroke-width="5"/>
  <path d="M84 16a50 50 0 0 1 4 88a46 46 0 0 0-4-88z" fill="#f2c14e"/>
  <rect x="31" y="34" width="58" height="54" rx="20" fill="#fff" stroke="#0f2744" stroke-width="4.5"/>
  <path d="M27 58a33 33 0 0 1 66 0" fill="none" stroke="#0f2744" stroke-width="5" stroke-linecap="round"/>
  <rect x="20" y="52" width="13" height="22" rx="6" fill="#2b8fc9" stroke="#0f2744" stroke-width="4"/>
  <rect x="87" y="52" width="13" height="22" rx="6" fill="#2b8fc9" stroke="#0f2744" stroke-width="4"/>
  <path d="M93 74q0 18-22 20" fill="none" stroke="#0f2744" stroke-width="4" stroke-linecap="round"/>
  <rect x="62" y="89" width="12" height="8" rx="4" fill="#2b8fc9" stroke="#0f2744" stroke-width="3"/>
  <g class="traza-ojos" fill="none" stroke="#0f2744" stroke-width="4.5" stroke-linecap="round">
    <path d="M44 58q5-6 10 0"/><path d="M66 58q5-6 10 0"/></g>
  <path class="traza-boca" d="M49 70q11 10 22 0" fill="none" stroke="#0f2744" stroke-width="4.5" stroke-linecap="round"/>
</svg>`;

const tzNorm = (s) => String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[¿?¡!.,;:"]/g, " ").replace(/\s+/g, " ").trim();
const tzRol = () => sesion.usuario && sesion.usuario.rol;

// ---------- Montaje: aparece al entrar y se va al salir ----------
function trazaMontar() {
  if (TRAZA.montada) return;
  TRAZA.montada = true;
  const raiz = document.createElement("div");
  raiz.id = "traza";
  raiz.innerHTML = `
    <section class="traza-panel oculto" id="traza-panel" role="dialog" aria-label="Traza, asistente de TrazaRed">
      <header class="traza-cabeza">
        <span class="traza-mini">${trazaAvatar("tz-g1")}</span>
        <div><strong>Traza</strong><span id="traza-estado">Asistente de TrazaRed · lista</span></div>
        <button class="traza-ico" id="traza-voz" title="Leer las respuestas en voz alta" aria-pressed="true">${tzIcono("altavoz")}</button>
        <button class="traza-ico" id="traza-cerrar" title="Cerrar" aria-label="Cerrar">${tzIcono("x")}</button>
      </header>
      <div class="traza-contexto oculto" id="traza-ctx"></div>
      <div class="traza-chat" id="traza-chat" aria-live="polite"></div>
      <div class="traza-chips" id="traza-chips"></div>
      <form class="traza-form" id="traza-form" autocomplete="off">
        <button type="button" class="traza-mic" id="traza-mic" title="Hablar con Traza" aria-label="Hablar con Traza">${tzIcono("mic")}</button>
        <input id="traza-texto" maxlength="400" placeholder="Escribe o toca el micrófono…" aria-label="Mensaje para Traza">
        <button type="submit" class="traza-enviar" aria-label="Enviar">${tzIcono("enviar")}</button>
      </form>
    </section>
    <button class="traza-boton" id="traza-boton" aria-label="Abrir a Traza, la asistente" title="Pregúntale a Traza">
      ${trazaAvatar("tz-g2")}<span class="traza-globo" id="traza-globo">¿Buscas a un paciente? Pregúntame</span>
    </button>`;
  document.body.appendChild(raiz);

  $("#traza-boton").addEventListener("click", () => trazaAbrir(!TRAZA.abierta));
  $("#traza-cerrar").addEventListener("click", () => trazaAbrir(false));
  $("#traza-voz").addEventListener("click", () => {
    TRAZA.hablar = !TRAZA.hablar;
    $("#traza-voz").setAttribute("aria-pressed", String(TRAZA.hablar));
    $("#traza-voz").classList.toggle("apagado", !TRAZA.hablar);
    if (!TRAZA.hablar) speechSynthesis.cancel();
  });
  $("#traza-form").addEventListener("submit", (e) => { e.preventDefault(); trazaEnviar($("#traza-texto").value, false); });
  $("#traza-mic").addEventListener("click", () => {
    if (TRAZA.escuchando || TRAZA.conversando) { trazaDetenerConversacion("Conversación terminada."); return; }
    TRAZA.conversando = true;
    trazaEscuchar();
  });
  setTimeout(() => $("#traza-globo") && $("#traza-globo").classList.add("visible"), 1200);
  setTimeout(() => $("#traza-globo") && $("#traza-globo").classList.remove("visible"), 7000);
}

function trazaDesmontar() {
  trazaDetenerConversacion();
  const r = $("#traza");
  if (r) r.remove();
  Object.assign(TRAZA, { abierta: false, montada: false, paciente: null, conversacion: "traza-" + Date.now().toString(36) });
}

function trazaAbrir(si) {
  TRAZA.abierta = si;
  $("#traza-panel").classList.toggle("oculto", !si);
  $("#traza-boton").classList.toggle("activo", si);
  $("#traza-globo").classList.remove("visible");
  if (si) {
    if (!$("#traza-chat").children.length) trazaBienvenida();
    $("#traza-texto").focus();
  } else trazaDetenerConversacion();
}

function trazaBienvenida() {
  const u = sesion.usuario, nombre = (u.nombre || "").split(" ")[0];
  const clin = TRAZA_CLINICOS.includes(u.rol);
  trazaMsg("traza", `¡Hola${nombre ? ", " + nombre : ""}! Soy Traza. ${clin
    ? "Dime el nombre o la cédula de un paciente y te llevo a su remisión. También puedo llevarte a cualquier sección o contarte cuántas remisiones hay pendientes."
    : "Puedo llevarte a cualquier sección del sistema. Dime, por ejemplo, «llévame a notificaciones»."}`, { hablar: false });
  trazaChips(clin
    ? ["¿Dónde está el paciente…?", "Remisiones pendientes", "Llévame a instituciones", "¿Qué puedes hacer?"]
    : ["Llévame al libro", "Notificaciones", "¿Qué puedes hacer?"]);
}

function trazaChips(lista) {
  $("#traza-chips").innerHTML = lista.map(s => `<button type="button">${esc(s)}</button>`).join("");
  $$("#traza-chips button").forEach(b => b.addEventListener("click", () => {
    if (b.textContent.endsWith("…?")) { $("#traza-texto").value = "¿Dónde está el paciente "; $("#traza-texto").focus(); return; }
    trazaEnviar(b.textContent, false);
  }));
}

function trazaMsg(quien, texto, { html = "", hablar = true } = {}) {
  const d = document.createElement("div");
  d.className = `traza-msg traza-${quien}`;
  d.innerHTML = `<p>${esc(texto)}</p>${html}`;
  $("#traza-chat").appendChild(d);
  $("#traza-chat").scrollTop = $("#traza-chat").scrollHeight;
  if (quien === "traza" && hablar) trazaDecir(texto);
  return d;
}

function trazaContexto() {
  const c = $("#traza-ctx");
  if (!TRAZA.paciente) { c.classList.add("oculto"); return; }
  c.classList.remove("oculto");
  c.innerHTML = `Paciente en contexto: <strong>${esc(TRAZA.paciente.nombre)}</strong>
    <button type="button" title="Quitar">${tzIcono("x")}</button>`;
  $("button", c).addEventListener("click", () => { TRAZA.paciente = null; trazaContexto(); });
}

// ---------- Entender lo que se pide ----------
const TRAZA_SECCIONES = [
  [/\b(libro|radicacion|inicio|remisiones)\b/, "inicio"], [/\bpacientes\b/, "pacientes"],
  [/\b(instituciones|hospitales|clinicas|ips)\b/, "hospitales"], [/\breportes?\b/, "reportes"],
  [/\b(interconsultas?|remitir)\b/, "remitir"], [/\bnotificacion(es)?\b/, "notificaciones"],
  [/\basistente clinico|agente\b/, "asistente"], [/\b(prediccion|urgencia)\b/, "prediccion"],
  [/\b(tablero|tendencia)\b/, "tendencia"], [/\bpronosticos?\b/, "pronosticos"],
  [/\b(agrupacion|clusters?|perfiles)\b/, "agrupacion"], [/\brevision\b/, "revision"],
  [/\brecupera(r|cion)\b/, "recuperacion"], [/\b(estado tecnico|tecnico)\b/, "tecnico"],
  [/\b(usuarios)\b/, "usuarios"], [/\bauditoria\b/, "auditoria"], [/\b(contable|contabilidad)\b/, "contabilidad"],
];
const TRAZA_ESTADOS = [[/pendiente/, "pendiente"], [/en gestion|gestionando|en proceso/, "en_gestion"],
  [/aceptad/, "aceptada"], [/rechazad/, "rechazada"], [/completad|trasladad/, "completada"]];
const TRAZA_RELLENO = new Set(("donde esta se encuentra encuentra encuentrame busca buscame buscar ubica ubicame abre abrir abreme " +
  "lleva llevame llevar muestrame mostrar mostrame ver quiero necesito por favor porfa el la los las al a de del " +
  "paciente pacientes senor senora sr sra don dona mi un una que como va estado esta en cedula documento cc numero " +
  "con identificacion traza oye hola ir ve vamos y me este esta ese esa").split(" "));

function trazaEntender(texto) {
  const t = tzNorm(texto);
  if (!t) return { tipo: "nada" };
  if (/^(gracias|listo|chao|adios|hasta luego|eso es todo|nada mas)\b/.test(t)) return { tipo: "despedida" };
  if (/^(hola|buenas|buenos dias|buenas tardes|hey)\b$/.test(t)) return { tipo: "saludo" };
  if (/que (puedes|sabes) hacer|ayuda|como funcionas|quien eres/.test(t)) return { tipo: "ayuda" };
  let m = t.match(/\bremision(?: numero| no| #)? (\d{1,6})\b/);
  if (m) return { tipo: "remision", id: +m[1] };
  const quiereIr = /\b(ir a|ve a|vamos a|llevame a|lleva me a|abre|abrir|abreme|muestrame|mostrar|entrar a|pasame a)\b/.test(t);
  const hayEstado = TRAZA_ESTADOS.some(([re]) => re.test(t));
  const cuenta = (/\b(cuant[ao]s|cuales|lista|resumen|hay|tengo|ver las|muestrame las)\b/.test(t) && (/\bremision/.test(t) || hayEstado))
    || (/\bremisiones\b/.test(t) && hayEstado);
  if (cuenta) {
    const e = TRAZA_ESTADOS.find(([re]) => re.test(t));
    return { tipo: "conteo", estado: e ? e[1] : null };
  }
  // ¿Pide un paciente? (palabra paciente, documento, o "dónde está X")
  const doc = t.match(/\b\d{6,12}\b/);
  const pidePaciente = doc || /^paciente\b|donde (esta|se encuentra)|\b(busca|buscame|buscar|encuentra|encuentrame|ubica|ubicame)\b|\bcomo va\b/.test(t)
    || (quiereIr && /\bpaciente\b/.test(t));
  if (pidePaciente) {
    const q = doc ? doc[0] : t.split(" ").filter(w => !TRAZA_RELLENO.has(w) && w.length > 1).join(" ");
    const sec = !doc && quiereIr && TRAZA_SECCIONES.find(([re]) => re.test(t));
    if (sec && (!q || q === "pacientes")) return { tipo: "ir", seccion: sec[1] };
    if (q.length >= 3) return { tipo: "paciente", q };
    if (/\bpaciente\b/.test(t) && !/donde|busca|encuentra/.test(t) && quiereIr) return { tipo: "ir", seccion: "pacientes" };
    if (TRAZA.paciente) return { tipo: "pregunta", texto };   // "¿cómo va el paciente?" con uno en contexto
    return { tipo: "falta_paciente" };
  }
  if (quiereIr || t.split(" ").length <= 3) {
    const sec = TRAZA_SECCIONES.find(([re]) => re.test(t));
    if (sec) return { tipo: "ir", seccion: sec[1] };
  }
  return { tipo: "pregunta", texto };
}

// ---------- Acciones ----------
async function trazaEnviar(texto, porVoz) {
  texto = String(texto || "").trim();
  if (!texto || TRAZA.ocupada) return;
  $("#traza-texto").value = "";
  $("#traza-chips").classList.add("oculto");            // más espacio para la conversación
  trazaMsg("yo", texto);
  const it = trazaEntender(texto);
  const rol = tzRol(), clin = TRAZA_CLINICOS.includes(rol);
  TRAZA.ocupada = true;
  try {
    switch (it.tipo) {
      case "saludo": trazaMsg("traza", "¡Hola! ¿A qué paciente buscamos o a dónde te llevo?"); break;
      case "despedida": trazaMsg("traza", "Con gusto. Aquí estoy si me necesitas."); trazaDetenerConversacion(); break;
      case "ayuda":
        trazaMsg("traza", clin
          ? "Puedo: encontrar a un paciente por nombre o cédula y llevarte a su remisión; abrir una remisión por su número; llevarte a cualquier sección; contarte las remisiones pendientes, en gestión o rechazadas; y pasarle tus preguntas clínicas al agente con el paciente en contexto. Toca el micrófono para hablarme."
          : "Puedo llevarte a cualquier sección de tu menú. Dime por ejemplo «llévame a notificaciones». Toca el micrófono para hablarme.");
        break;
      case "falta_paciente": trazaMsg("traza", "¿Cómo se llama el paciente o cuál es su cédula?"); break;
      case "remision": await trazaAbrirRemision(it.id); break;
      case "ir": trazaIr(it.seccion); break;
      case "conteo": await trazaConteo(it.estado, clin); break;
      case "paciente":
        if (!clin) { trazaMsg("traza", "Tu rol no tiene acceso a los datos de pacientes."); break; }
        await trazaBuscarPaciente(it.q); break;
      default: await trazaPreguntar(it.texto, clin);
    }
  } catch (e) {
    trazaMsg("traza", e.status === 403 ? "Tu rol no tiene permiso para eso." : `No pude hacerlo: ${e.message}`);
  } finally {
    TRAZA.ocupada = false;
    trazaEstado("lista");
  }
}

function trazaIr(seccion) {
  const s = SECCIONES[seccion];
  if (!s || !s.roles.includes(tzRol())) { trazaMsg("traza", "Esa sección no está en tu menú."); return; }
  const titulo = seccion === "inicio" && tzRol() === "paciente" ? "Mi remisión" : s.titulo;
  trazaMsg("traza", `Listo, te llevo a ${titulo}.`);
  ir(seccion);
}

async function trazaAbrirRemision(id) {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no abre formatos de remisión."); return; }
  const r = (await remisiones()).find(x => x.id === id);
  if (!r) { trazaMsg("traza", `No encontré la remisión ${id} en el libro.`); return; }
  trazaMsg("traza", `Abro la remisión ${id} de ${r.paciente_nombre}: está ${ESTADOS[r.estado].toLowerCase()}.`);
  TRAZA.paciente = { id: r.paciente_id, nombre: r.paciente_nombre }; trazaContexto();
  ir("ficha", { remision: id });
}

function trazaUltimaRemision(lista, pid) {
  return lista.filter(r => r.paciente_id === pid && r.activo !== false)
    .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)) || b.id - a.id)[0];
}

async function trazaBuscarPaciente(q) {
  trazaEstado("buscando…");
  // Búsqueda sin tildes y por palabras sueltas ("munoz" encuentra a "Muñoz";
  // "marleny carabali" encuentra a "Marleny Balanta Carabalí").
  const [todos, rems] = await Promise.all([pacientes(), remisiones()]);
  const palabras = tzNorm(q).split(" ").filter(Boolean);
  const encontrados = todos.filter(p => {
    const nom = tzNorm(p.nombre), doc = String(p.documento);
    return palabras.every(w => /^\d+$/.test(w) ? doc.includes(w) : nom.split(" ").some(x => x.startsWith(w)));
  });
  if (!encontrados.length) {
    trazaMsg("traza", `No encontré ningún paciente con «${q}». Prueba con el apellido o la cédula.`);
    return;
  }
  const filas = encontrados.slice(0, 5).map(p => ({ p, r: trazaUltimaRemision(rems, p.id) }));
  const tarjetas = filas.map(({ p, r }) => `<button type="button" class="traza-pac" data-pid="${p.id}" ${r ? `data-rem="${r.id}"` : ""}>
      <span class="avatar">${esc(iniciales(p.nombre))}</span>
      <span class="traza-pac-txt"><strong>${esc(p.nombre)}</strong>
        <small>CC ${esc(p.documento)} · ${esc(p.eps)}</small>
        ${r ? `<small><span class="traza-est est-${esc(r.estado)}">${esc(ESTADOS[r.estado])}</span> → ${esc(r.institucion_destino)}</small>` : `<small>Sin remisiones en el libro</small>`}
      </span>${tzIcono("flecha")}</button>`).join("");
  const extra = encontrados.length > 5 ? `<p class="traza-nota">Hay ${encontrados.length} coincidencias; dime el apellido completo o la cédula para afinar.</p>` : "";

  if (filas.length === 1) {
    const { p, r } = filas[0];
    TRAZA.paciente = { id: p.id, nombre: p.nombre }; trazaContexto();
    if (r) {
      trazaMsg("traza", `Encontré a ${p.nombre}. Su última remisión está ${ESTADOS[r.estado].toLowerCase()}, hacia ${r.institucion_destino}. Te llevo a su ficha.`, { html: tarjetas });
      trazaClicTarjetas();
      ir("ficha", { remision: r.id });
    } else {
      trazaMsg("traza", `Encontré a ${p.nombre}, pero no tiene remisiones en el libro. Te lo muestro en Pacientes.`, { html: tarjetas });
      trazaClicTarjetas();
      trazaVerEnPacientes(p.documento);
    }
    return;
  }
  trazaMsg("traza", `Encontré ${encontrados.length} pacientes con «${q}». ¿Cuál es? Toca su tarjeta.`, { html: tarjetas + extra });
  trazaClicTarjetas();
}

function trazaClicTarjetas() {
  const ultimo = $("#traza-chat").lastElementChild;
  $$(".traza-pac", ultimo).forEach(b => b.addEventListener("click", () => {
    const nombre = $("strong", b).textContent;
    TRAZA.paciente = { id: +b.dataset.pid, nombre }; trazaContexto();
    if (b.dataset.rem) { ir("ficha", { remision: +b.dataset.rem }); trazaMsg("traza", `Abrí la ficha de ${nombre}.`, { hablar: false }); }
    else trazaVerEnPacientes($("small", b).textContent.replace(/^CC (\d+).*/, "$1"));
  }));
}

function trazaVerEnPacientes(q) {
  ir("pacientes");
  const inp = $("#p-buscar");
  if (inp) { inp.value = q; inp.dispatchEvent(new Event("input")); }
}

async function trazaConteo(estado, clin) {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no ve el libro de remisiones completo."); return; }
  const lista = activas(await remisiones());
  const n = (e) => lista.filter(r => r.estado === e).length;
  if (!estado) {
    trazaMsg("traza", `En el libro hay ${n("pendiente")} pendientes, ${n("en_gestion")} en gestión, ${n("aceptada")} aceptadas, ${n("rechazada")} rechazadas y ${n("completada")} completadas.`,
      { html: `<div class="traza-acciones"><button type="button" data-ir="abiertas">Ver las abiertas en el libro</button></div>` });
  } else {
    const sel = lista.filter(r => r.estado === estado).sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
    const top = sel.slice(0, 5).map(r => `<button type="button" class="traza-pac" data-rem="${r.id}" data-pid="${r.paciente_id}">
        <span class="avatar">${esc(iniciales(r.paciente_nombre))}</span>
        <span class="traza-pac-txt"><strong>${esc(r.paciente_nombre)}</strong><small>Remisión ${r.id} · ${esc(fechaCorta(r.fecha_solicitud))}</small>
        <small>→ ${esc(r.institucion_destino)}</small></span>${tzIcono("flecha")}</button>`).join("");
    trazaMsg("traza", `Hay ${sel.length} remisiones ${ESTADOS[estado].toLowerCase()}${sel.length > 1 ? "s" : ""}`.replace("en gestións", "en gestión") + (sel.length ? ". Estas son las más recientes:" : "."), { html: top });
  }
  const ultimo = $("#traza-chat").lastElementChild;
  $$(".traza-pac", ultimo).forEach(b => b.addEventListener("click", () => {
    TRAZA.paciente = { id: +b.dataset.pid, nombre: $("strong", b).textContent }; trazaContexto();
    ir("ficha", { remision: +b.dataset.rem });
  }));
  $$("[data-ir]", ultimo).forEach(b => b.addEventListener("click", () => ir("inicio", { vista: b.dataset.ir })));
}

async function trazaPreguntar(texto, clin) {
  if (!["medico", "especialista"].includes(tzRol())) {
    trazaMsg("traza", clin ? "Esa pregunta la responde el agente clínico, que es para médicos y especialistas. Yo puedo buscar pacientes y llevarte a cualquier sección."
      : "No entendí. Puedo llevarte a cualquier sección de tu menú: dime, por ejemplo, «llévame a notificaciones».");
    return;
  }
  trazaEstado("pensando…");
  const espera = trazaMsg("traza", "Le pregunto al agente clínico…", { hablar: false });
  espera.classList.add("traza-esperando");
  const r = await API.post("/agente/chat", { mensaje: texto, conversacion_id: TRAZA.conversacion, paciente_id: TRAZA.paciente ? TRAZA.paciente.id : null });
  espera.remove();
  if (r.estado === "respondido") trazaMsg("traza", (r.respuesta || "(sin texto)").replace(/[*#_`]/g, ""));
  else if (r.estado === "pendiente_aprobacion") {
    trazaMsg("traza", "El agente propone una acción que necesita tu aprobación. Te llevo al Asistente clínico para que decidas.",
      { html: `<div class="traza-acciones"><button type="button" data-ast>Abrir el Asistente clínico</button></div>` });
    $("[data-ast]", $("#traza-chat").lastElementChild).addEventListener("click", () => {
      if (typeof AST !== "undefined") AST.conversacion = TRAZA.conversacion;
      ir("asistente", { paciente: TRAZA.paciente && TRAZA.paciente.id });
    });
  } else trazaMsg("traza", r.detalle || "El agente no pudo responder. Intenta de nuevo.");
}

// ---------- Voz ----------
function trazaEstado(txt) { const e = $("#traza-estado"); if (e) e.textContent = `Asistente de TrazaRed · ${txt}`; }

function trazaVozEs() {
  const voces = speechSynthesis.getVoices().filter(v => /^es/i.test(v.lang));
  return voces.find(v => /es-CO/i.test(v.lang)) || voces.find(v => /es-(MX|US|419)/i.test(v.lang)) || voces[0] || null;
}

function trazaDecir(texto) {
  const seguir = () => { if (TRAZA.conversando && TRAZA.abierta) setTimeout(trazaEscuchar, 250); };
  if (!TRAZA.hablar || !("speechSynthesis" in window)) { seguir(); return; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(texto.length > 320 ? texto.slice(0, 320).replace(/\s\S*$/, "") + "…" : texto);
  const v = trazaVozEs();
  if (v) u.voice = v;
  u.lang = v ? v.lang : "es-CO";
  u.rate = 1.03;
  const b = $("#traza-boton"), p = $("#traza-panel");
  u.onstart = () => { b && b.classList.add("hablando"); p && p.classList.add("hablando"); trazaEstado("hablando"); };
  u.onend = u.onerror = () => { b && b.classList.remove("hablando"); p && p.classList.remove("hablando"); trazaEstado("lista"); seguir(); };
  speechSynthesis.speak(u);
}

function trazaEscuchar() {
  if (!TRAZA_SR) {
    TRAZA.conversando = false;
    trazaMsg("traza", "Este navegador no reconoce voz. Abre TrazaRed en Chrome o Edge para hablarme; mientras tanto, escríbeme.", { hablar: false });
    return;
  }
  if (TRAZA.escuchando || !TRAZA.abierta) return;
  speechSynthesis.cancel();
  const sr = new TRAZA_SR();
  sr.lang = "es-CO"; sr.interimResults = true; sr.continuous = false; sr.maxAlternatives = 1;
  let final = "";
  sr.onstart = () => { TRAZA.escuchando = true; $("#traza-mic").classList.add("escuchando"); $("#traza-boton").classList.add("escuchando"); trazaEstado("escuchando…"); };
  sr.onresult = (ev) => {
    let parcial = "";
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      if (ev.results[i].isFinal) final += ev.results[i][0].transcript; else parcial += ev.results[i][0].transcript;
    }
    $("#traza-texto").value = (final + parcial).trim();
  };
  sr.onerror = (ev) => {
    const msg = { "not-allowed": "No tengo permiso para el micrófono. Haz clic en el candado de la barra de direcciones y permite el micrófono.",
      "service-not-allowed": "El navegador bloqueó el reconocimiento de voz. Usa Chrome o Edge.",
      network: "Este navegador no deja usar el reconocimiento de voz (pasa en Brave). Abre TrazaRed en Chrome o Edge.",
      "audio-capture": "No encuentro un micrófono. Revisa que esté conectado y activo en Windows." }[ev.error];
    if (msg) { trazaMsg("traza", msg, { hablar: false }); TRAZA.conversando = false; }
    else if (ev.error === "no-speech" && TRAZA.conversando) { trazaDetenerConversacion("No te escuché; toca el micrófono cuando quieras."); }
  };
  sr.onend = () => {
    TRAZA.escuchando = false; TRAZA.sr = null;
    const m = $("#traza-mic"); if (m) m.classList.remove("escuchando");
    const b = $("#traza-boton"); if (b) b.classList.remove("escuchando");
    trazaEstado("lista");
    if (final.trim()) trazaEnviar(final, true);
  };
  TRAZA.sr = sr;
  try { sr.start(); } catch { /* ya estaba escuchando */ }
}

function trazaDetenerConversacion(aviso) {
  TRAZA.conversando = false;
  if (TRAZA.sr) { try { TRAZA.sr.abort(); } catch { /* nada */ } }
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (aviso && $("#traza-chat")) trazaMsg("traza", aviso, { hablar: false });
}

// ---------- Iconos propios del widget ----------
function tzIcono(n) {
  const p = {
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    enviar: '<path d="M4 12l16-8-6 16-2-7z"/>',
    altavoz: '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    flecha: '<path d="M9 6l6 6-6 6"/>',
  }[n];
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
}

// Aparece cuando se ve la aplicación (después del login) y se quita al salir.
(function trazaVigilar() {
  const app = document.getElementById("vista-app");
  if (!app) return;
  const revisar = () => {
    const visible = !app.classList.contains("oculto") && sesion.usuario;
    if (visible && !TRAZA.montada) trazaMontar();
    else if (!visible && TRAZA.montada) trazaDesmontar();
  };
  new MutationObserver(revisar).observe(app, { attributes: true, attributeFilter: ["class"] });
  if ("speechSynthesis" in window) speechSynthesis.getVoices();   // precarga de voces
  revisar();
})();
