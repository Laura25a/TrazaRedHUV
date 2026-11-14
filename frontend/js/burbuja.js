// ==========================================================================
// TrazaRed HUV — "Traza", la asistente flotante (esquina inferior derecha)
// ==========================================================================
// Está en todas las pantallas. Se le escribe o se le HABLA, como a un colega, y
// responde corto o te lleva al lugar:
//   · Pacientes  «¿cómo va Marleny Balanta?» · «busca la cédula 1175373460»
//                → te abre su ficha y te resume lo que importa en dos líneas.
//   · Mi trabajo «¿qué tengo pendiente?» · «alertas» · «remisiones en riesgo» · «las de hoy»
//   · Clínica    «¿qué tan grave está?» · «¿a dónde lo remito?» (agente con el paciente
//                en contexto) · «criterios de qSOFA» (guías, con sus fuentes)
//   · Hacer      «radicar remisión» · «nuevo paciente» · «dictar llamada» → abre el formulario
//   · Navegar    «llévame a instituciones» · «abre la remisión 45»
//   · Admin      «usuarios bloqueados» · «estado del sistema» · «¿la auditoría está
//                íntegra?» · «incidentes» y, en cada respuesta del agente, el detalle técnico.
// AL MÉDICO le llega la respuesta y sus fuentes, no el proceso. AL ADMINISTRADOR,
// además, la traza completa (herramientas, argumentos y lo enviado al LLM).
// Búsqueda, navegación, conteos y resúmenes se resuelven AQUÍ, con la API del
// sistema: nombres y documentos no van a ningún modelo de lenguaje. Al agente solo
// le llega el número del paciente; el servidor lo convierte en seudónimo.
// Cuando abres una ficha, Traza ya sabe de qué paciente hablas.
//
// Voz: dictado del navegador (Chrome/Edge) y lectura en voz alta
// (speechSynthesis). En "conversación" Traza vuelve a escuchar después de
// responder, hasta que digas "gracias", "listo" o toques el micrófono.
"use strict";

const TRAZA = {
  abierta: false, montada: false, ocupada: false,
  paciente: null,            // {id, nombre} del paciente en contexto
  remision: null,            // su remisión abierta (para resumir y para las acciones)
  propuesta: null,           // {conv, destino}: remisión propuesta por el agente esperando decisión
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
  Object.assign(TRAZA, { abierta: false, montada: false, paciente: null, remision: null, propuesta: null, conversacion: "traza-" + Date.now().toString(36) });
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
  const u = sesion.usuario, nombre = primerNombre(u.nombre);
  const clin = TRAZA_CLINICOS.includes(u.rol);
  trazaMsg("traza", `¡Hola${nombre ? ", " + nombre : ""}! Soy Traza. ${clin
    ? (TRAZA.paciente ? `Veo que tienes abierto a ${primerNombre(TRAZA.paciente.nombre)}. Pregúntame por él o por lo que necesites.`
      : "Dime un paciente y te llevo a su ficha, o pregúntame lo que necesites: te respondo corto.")
    : "Te llevo a cualquier sección. Dime, por ejemplo, «llévame a notificaciones»."}`, { hablar: false });
  trazaSugerir(clin
    ? (TRAZA.paciente ? ["¿Cómo va?", "¿Qué tan grave está?", "¿Qué tengo pendiente?"]
      : ["¿Dónde está el paciente…?", "¿Qué tengo pendiente?", u.rol === "admin" ? "Estado del sistema" : "Remisiones en riesgo", "¿Qué puedes hacer?"])
    : ["Llévame al libro", "Notificaciones", "¿Qué puedes hacer?"]);
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
  if (!c) return;
  if (!TRAZA.paciente) { c.classList.add("oculto"); return; }
  c.classList.remove("oculto");
  c.innerHTML = `Paciente en contexto: <strong>${esc(TRAZA.paciente.nombre)}</strong>
    <button type="button" title="Quitar">${tzIcono("x")}</button>`;
  $("button", c).addEventListener("click", () => { TRAZA.paciente = null; TRAZA.remision = null; trazaContexto(); });
}

// ---------- Entender lo que se pide ----------
const TRAZA_SECCIONES = [
  [/\b(libro|radicacion|inicio|remisiones)\b/, "inicio"], [/\bpacientes\b/, "pacientes"],
  [/\b(instituciones|hospitales|clinicas|ips)\b/, "hospitales"], [/\breportes? de ia\b|\brevision\b/, "revision"],
  [/\breportes?\b/, "reportes"],
  [/\b(interconsultas?|remitir)\b/, "remitir"], [/\bnotificacion(es)?\b/, "notificaciones"],
  [/\basistente clinico|agente\b/, "asistente"], [/\b(prediccion|urgencia)\b/, "prediccion"],
  [/\b(tablero|tendencia)\b/, "tendencia"], [/\bpronosticos?\b/, "pronosticos"],
  [/\b(agrupacion|clusters?|perfiles)\b/, "agrupacion"],
  [/\brecupera(r|cion)\b|\bpapelera\b/, "recuperacion"], [/\b(estado tecnico|tecnico)\b/, "tecnico"],
  [/\b(interoperabilidad|interop|socios?|institucion socia|recibidas|enviadas)\b/, "interop"],
  [/\bseguridad\b/, "seguridad"],
  [/\b(usuarios)\b/, "usuarios"], [/\bauditoria\b/, "auditoria"], [/\b(contable|contabilidad|facturas?)\b/, "contabilidad"],
];
const TRAZA_ESTADOS = [[/pendiente/, "pendiente"], [/en gestion|gestionando|en proceso/, "en_gestion"],
  [/aceptad/, "aceptada"], [/rechazad/, "rechazada"], [/completad|trasladad/, "completada"]];
const TRAZA_RELLENO = new Set(("donde esta se encuentra encuentra encuentrame busca buscame buscar ubica ubicame abre abrir abreme " +
  "lleva llevame llevar muestrame mostrar mostrame ver quiero necesito por favor porfa el la los las al a de del " +
  "paciente pacientes senor senora sr sra don dona mi un una que como va estado esta en cedula documento cc numero " +
  "con identificacion traza oye hola ir ve vamos y me este esta ese esa tiene resumen dame dime sobre cual es su " +
  "sigue anda ficha remision historia informacion datos").split(" "));
// Preguntas que se responden con las guías (RAG), sin necesidad de un paciente
const TRAZA_GUIA = /\b(guia|guias|protocolo|criterios?|qsofa|news2?|sepsis|triage|ktas|glasgow|hipoglucemia|infarto|sindrome coronario|evc|acv|norma|ley|resolucion|que es|que significa|cuando se|como se (hace|radica|remite)|que hago|requisitos?|conducta|manejo)\b/;

function trazaEntender(texto) {
  const t = tzNorm(texto);
  const rol = tzRol();
  if (!t) return { tipo: "nada" };
  if (TRAZA.propuesta) {
    if (/^(si|sii|dale|ok|okay|de acuerdo|hazlo|claro|correcto|apruebo|aprobar|apruebala|aprobada|radicala|radicar|radica|acepto|aceptar|aceptala|confirmo|confirmar)\b|^si,? (radicala|radica|aprueba|dale|hazlo)/.test(t)) return { tipo: "decidir", aprobar: true };
    if (/^(no|nop|nel|descarta|descartala|descartar|rechaza|rechazala|rechazar|cancela|cancelala|mejor no)\b/.test(t)) return { tipo: "decidir", aprobar: false, motivo: texto.replace(/^\s*no[,.]?\s*/i, "").trim() };
    if (/\b(ya (la |lo )?(acepte|aprobe|hice|decidi|radique|rechace|descarte)|ya esta|listo ya|ya)\b/.test(t)) return { tipo: "revisar_propuesta" };
  }
  if (/^(gracias|listo|chao|adios|hasta luego|eso es todo|nada mas|muchas gracias)\b/.test(t)) return { tipo: "despedida" };
  if (/^(hola|buenas|buenos dias|buenas tardes|buenas noches|hey)\b$/.test(t)) return { tipo: "saludo" };
  if (/que (puedes|sabes) hacer|^ayuda\b|como funcionas|quien eres|para que sirves/.test(t)) return { tipo: "ayuda" };

  let m = t.match(/\bremision(?: numero| no| #)? (\d{1,6})\b/);
  if (m) return { tipo: "remision", id: +m[1] };

  // Cosas que se HACEN: te lleva y abre el formulario
  if (/\b(nuevo|crear|registrar|agregar) (un )?paciente\b/.test(t)) return { tipo: "accion", accion: "nuevo_paciente" };
  if (/\b(radicar|nueva|crear|registrar) (una )?remision\b|\bradicar\b/.test(t)) return { tipo: "accion", accion: "radicar" };
  if (/\b(anotar|registrar) (una )?(gestion|llamada|contacto)\b/.test(t)) return { tipo: "accion", accion: "gestion" };
  if (/\bdictar\b/.test(t)) return { tipo: "accion", accion: "dictar" };
  if (/\b(remit\w+|envia\w*) (a|al|a la) (institucion|clinica|hospital)? ?(socia|socio)\b|\binstitucion socia\b/.test(t) && TRAZA.remision) return { tipo: "accion", accion: "socio" };

  // Mi trabajo
  if (/\balertas?\b/.test(t)) return { tipo: "alertas" };
  if (/que (tengo|hay) (pendiente|por hacer|para hoy)|mis pendientes|mi trabajo|que me falta|por donde empiezo/.test(t)) return { tipo: "pendientes" };
  if (/\bnotificaciones?\b/.test(t) && /\b(cuantas|tengo|sin leer|nuevas|hay)\b/.test(t)) return { tipo: "notificaciones" };
  if (/\b(en riesgo|estancad|demorad|sin movimiento|atrasad|llevan mas|mas antigu|mas viej)/.test(t)) return { tipo: "riesgo" };
  if (/\b(remisiones|radicad\w*) (de |del dia de )?hoy\b|\bhoy\b.*\bremision/.test(t)) return { tipo: "hoy" };
  if (/\b(recibidas?|llegaron|llego) (de|desde) (otra|la otra|el otro|otro)|\bremisiones externas\b/.test(t)) return { tipo: "ir", seccion: "interop" };

  // Administrador: el sistema por dentro
  if (rol === "admin") {
    if (/\b(usuarios?|cuentas?) bloquead/.test(t) || /\bbloqueados\b/.test(t)) return { tipo: "admin", que: "bloqueados" };
    if (/\b(estado|salud|como esta) (del |de los )?(sistema|servicios?)\b|\bservicios\b.*\b(arriba|caidos?|funcionan)/.test(t)) return { tipo: "admin", que: "servicios" };
    if (/\bauditoria\b.*\b(integra|alterad|verific|cadena|manipul)|\bverifica\w* (la )?(auditoria|cadena)/.test(t)) return { tipo: "admin", que: "cadena" };
    if (/\b(incidentes?|ataques?|inyeccion|cifrad\w*|bloqueos?)\b/.test(t) || /\bcomo (esta|va) la seguridad\b/.test(t)) return { tipo: "admin", que: "seguridad" };
    if (/\b(que se (le )?envio al (modelo|llm)|payload|invocaciones)\b/.test(t)) return { tipo: "ir", seccion: "seguridad", params: { pestana: "llm" } };
  }

  const hayEstado = TRAZA_ESTADOS.some(([re]) => re.test(t));
  const quiereIr = /\b(ir a|ve a|vamos a|llevame a|lleva me a|abre|abrir|abreme|muestrame|mostrar|entrar a|pasame a|donde (esta|queda|encuentro) (el|la|lo de|los|las) (modulo|seccion|pantalla|parte|opcion))\b/.test(t);
  const cuenta = (/\b(cuant[ao]s|cuales|lista|resumen|hay|tengo|ver las|muestrame las)\b/.test(t) && (/\bremision/.test(t) || hayEstado))
    || (/\bremisiones\b/.test(t) && hayEstado);
  if (cuenta) {
    const e = TRAZA_ESTADOS.find(([re]) => re.test(t));
    return { tipo: "conteo", estado: e ? e[1] : null };
  }

  // ¿Dónde se hace algo? ("¿dónde subo una radiografía?", "¿dónde veo las facturas?")
  if (/\b(donde|como) (subo|cargo|adjunto)\b.*\b(imagen|radiografia|rx|tac|foto)/.test(t)) return { tipo: "como", que: "imagen" };
  if (/\b(donde|como) (veo|descargo|saco|imprimo)\b.*\b(historia|pdf|constancia)/.test(t)) return { tipo: "como", que: "historia" };

  // ¿Es sobre un paciente?
  const doc = t.match(/\b\d{6,12}\b/);
  const pideResumen = /\b(como (esta|va|sigue|anda)|que tiene|que le paso|resumen|estado de|situacion de|informacion de|datos de)\b/.test(t);
  const pideUbicar = /^paciente\b|donde (esta|se encuentra)|\b(busca|buscame|buscar|encuentra|encuentrame|ubica|ubicame)\b/.test(t)
    || (quiereIr && /\bpaciente\b/.test(t));
  if (doc || pideUbicar || pideResumen) {
    const q = doc ? doc[0] : t.split(" ").filter(w => !TRAZA_RELLENO.has(w) && w.length > 1).join(" ");
    const sec = !doc && quiereIr && TRAZA_SECCIONES.find(([re]) => re.test(t));
    if (sec && (!q || q === "pacientes")) return { tipo: "ir", seccion: sec[1] };
    const esPregunta = /\b(grave|urgente|critico|riesgo|remit|guia|convenio|signos|glucosa|saturacion)\b/.test(t);
    if (q.length >= 3 && !esPregunta) return { tipo: "paciente", q, resumen: pideResumen && !pideUbicar };
    if (TRAZA.paciente && pideResumen && !esPregunta) return { tipo: "resumen" };
    if (TRAZA.paciente) return { tipo: "pregunta", texto };
    if (/\bpaciente\b/.test(t) && quiereIr && !pideUbicar) return { tipo: "ir", seccion: "pacientes" };
    return { tipo: "falta_paciente" };
  }
  if (quiereIr || t.split(" ").length <= 3) {
    const sec = TRAZA_SECCIONES.find(([re]) => re.test(t));
    if (sec) return { tipo: "ir", seccion: sec[1] };
  }
  // Convenios por EPS: se responde con el historial del libro
  m = t.match(/\bconvenio\b.*\b(?:con|de|para) (?:la )?(?:eps )?([a-z ]{4,30})$/);
  if (m && !/\b(este|su|el paciente)\b/.test(t)) return { tipo: "convenio", eps: m[1].trim() };
  if (TRAZA_GUIA.test(t) && !/\b(este|esta|el|la|su) paciente\b|\beste caso\b/.test(t)) return { tipo: "guia", texto };
  return { tipo: "pregunta", texto };
}

// ---------- Responder ----------
async function trazaEnviar(texto, porVoz) {
  texto = String(texto || "").trim();
  if (!texto || TRAZA.ocupada) return;
  $("#traza-texto").value = "";
  $("#traza-chips").classList.add("oculto");
  trazaMsg("yo", texto);
  const it = trazaEntender(texto);
  const rol = tzRol(), clin = TRAZA_CLINICOS.includes(rol);
  TRAZA.ocupada = true;
  try {
    switch (it.tipo) {
      case "saludo": trazaMsg("traza", clin ? "¡Hola! Dime un paciente, una sección o una duda clínica." : "¡Hola! ¿A dónde te llevo?"); break;
      case "decidir": await trazaDecidir(it.aprobar, it.motivo); break;
      case "revisar_propuesta": await trazaRevisarPropuesta(); break;
      case "despedida": trazaMsg("traza", "Con gusto. Aquí estoy si me necesitas."); trazaDetenerConversacion(); break;
      case "ayuda": trazaAyuda(clin, rol); break;
      case "falta_paciente": trazaMsg("traza", "¿De qué paciente? Dime su nombre o su cédula."); break;
      case "remision": await trazaAbrirRemision(it.id); break;
      case "ir": trazaIr(it.seccion, it.params); break;
      case "accion": await trazaAccion(it.accion); break;
      case "como": trazaComo(it.que); break;
      case "conteo": await trazaConteo(it.estado); break;
      case "alertas": await trazaAlertas(clin); break;
      case "pendientes": await trazaPendientes(clin); break;
      case "notificaciones": await trazaNotificaciones(); break;
      case "riesgo": await trazaRiesgo(); break;
      case "hoy": await trazaHoy(); break;
      case "convenio": await trazaConvenio(it.eps); break;
      case "admin": await trazaAdmin(it.que); break;
      case "resumen": await trazaResumen(TRAZA.paciente.id, TRAZA.remision); break;
      case "paciente":
        if (!clin) { trazaMsg("traza", "Tu rol no tiene acceso a los datos de pacientes."); break; }
        await trazaBuscarPaciente(it.q, it.resumen); break;
      case "guia": await trazaGuia(it.texto, clin); break;
      default: await trazaPreguntar(it.texto, clin);
    }
  } catch (e) {
    trazaMsg("traza", e.status === 403 ? (e.message || "Tu rol no tiene permiso para eso.")
      : e.status === 429 ? "Voy muy rápido para el sistema. Dame unos segundos y vuelve a preguntarme."
      : `No pude hacerlo: ${e.message}`);
  } finally {
    TRAZA.ocupada = false;
    trazaEstado("lista");
  }
}

function trazaAyuda(clin, rol) {
  trazaMsg("traza", clin
    ? "Pregúntame como le preguntarías a un colega. Te respondo corto o te llevo al lugar."
    : "Te llevo a cualquier sección de tu menú. Dime, por ejemplo, «llévame a notificaciones».", {
    html: clin ? `<ul class="traza-ayuda">
      <li><strong>Pacientes:</strong> «¿cómo va Marleny Balanta?», «busca la cédula 1175373460»</li>
      <li><strong>Mi trabajo:</strong> «¿qué tengo pendiente?», «alertas», «remisiones en riesgo»</li>
      <li><strong>Clínica:</strong> «¿qué tan grave está?», «¿a dónde lo remito?», «criterios de qSOFA»</li>
      <li><strong>Hacer:</strong> «radicar remisión», «nuevo paciente», «dictar llamada»</li>
      ${rol === "admin" ? `<li><strong>Sistema:</strong> «usuarios bloqueados», «estado del sistema», «¿la auditoría está íntegra?», «incidentes»</li>` : ""}
      </ul>` : "" });
  trazaSugerir(clin ? ["¿Qué tengo pendiente?", "Alertas", "Remisiones en riesgo"] : ["Notificaciones", "Llévame al libro"]);
}

function trazaIr(seccion, params) {
  const s = SECCIONES[seccion];
  if (!s || !s.roles.includes(tzRol())) { trazaMsg("traza", "Esa sección no está en tu menú."); return; }
  const titulo = seccion === "inicio" && tzRol() === "paciente" ? "Mi remisión" : s.titulo;
  trazaMsg("traza", `Listo, te llevo a ${titulo}.`);
  ir(seccion, params || {});
}

async function trazaAbrirRemision(id) {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no abre formatos de remisión."); return; }
  const r = (await remisiones()).find(x => x.id === id);
  if (!r) { trazaMsg("traza", `No encontré la remisión ${id} en el libro.`); return; }
  trazaMsg("traza", `Abro la remisión ${id} de ${r.paciente_nombre}: está ${ESTADOS[r.estado].toLowerCase()}.`);
  trazaFijar(r.paciente_id, r.paciente_nombre, id);
  ir("ficha", { remision: id });
  trazaSugerir(["¿Cómo va?", "¿Qué tan grave está?"]);
}

function trazaFijar(pid, nombre, remisionId) {
  TRAZA.paciente = { id: pid, nombre };
  TRAZA.remision = remisionId || null;
  trazaContexto();
}

function trazaUltimaRemision(lista, pid) {
  return lista.filter(r => r.paciente_id === pid && r.activo !== false)
    .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)) || b.id - a.id)[0];
}

const trazaTarjeta = (nombre, lineas, attrs) => `<button type="button" class="traza-pac" ${attrs}>
  <span class="avatar">${esc(iniciales(nombre))}</span>
  <span class="traza-pac-txt"><strong>${esc(nombre)}</strong>${lineas.map(l => `<small>${l}</small>`).join("")}</span>${tzIcono("flecha")}</button>`;

async function trazaBuscarPaciente(q, conResumen) {
  trazaEstado("buscando…");
  // Sin tildes y por palabras sueltas: "munoz" encuentra a "Muñoz". Todo en el navegador,
  // con lo que la API ya le entrega a este rol: ningún nombre va a un modelo de lenguaje.
  const [todos, rems] = await Promise.all([pacientes(), remisiones()]);
  const palabras = tzNorm(q).split(" ").filter(Boolean);
  const encontrados = todos.filter(p => {
    const nom = tzNorm(p.nombre), doc = String(p.documento);
    return palabras.every(w => /^\d+$/.test(w) ? doc.includes(w) : nom.split(" ").some(x => x.startsWith(w)));
  });
  if (!encontrados.length) {
    trazaMsg("traza", `No encontré ningún paciente con «${q}». Prueba con un apellido o con la cédula completa.`);
    return;
  }
  const filas = encontrados.slice(0, 5).map(p => ({ p, r: trazaUltimaRemision(rems, p.id) }));
  if (filas.length === 1) {
    const { p, r } = filas[0];
    trazaFijar(p.id, p.nombre, r && r.id);
    if (r) { ir("ficha", { remision: r.id }); await trazaResumen(p.id, r.id, true); }
    else {
      trazaMsg("traza", `${p.nombre} no tiene remisiones en el libro. Te lo muestro en Pacientes.`);
      trazaVerEnPacientes(p.documento);
      trazaSugerir(["Radicar remisión"]);
    }
    return;
  }
  const tarjetas = filas.map(({ p, r }) => trazaTarjeta(p.nombre, [
    `CC ${esc(p.documento)} · ${esc(p.eps)}`,
    r ? `<span class="traza-est est-${esc(r.estado)}">${esc(ESTADOS[r.estado])}</span> → ${esc(r.institucion_destino)}` : "Sin remisiones en el libro",
  ], `data-pid="${p.id}" data-doc="${esc(p.documento)}" ${r ? `data-rem="${r.id}"` : ""}`)).join("");
  trazaMsg("traza", `Hay ${encontrados.length} pacientes con «${q}». ¿Cuál es?`, {
    html: tarjetas + (encontrados.length > 5 ? `<p class="traza-nota">Dime el apellido completo o la cédula para afinar.</p>` : "") });
  $$(".traza-pac", $("#traza-chat").lastElementChild).forEach(b => b.addEventListener("click", async () => {
    const nombre = $("strong", b).textContent;
    trazaFijar(+b.dataset.pid, nombre, b.dataset.rem ? +b.dataset.rem : null);
    if (b.dataset.rem) { ir("ficha", { remision: +b.dataset.rem }); await trazaResumen(+b.dataset.pid, +b.dataset.rem, true); }
    else trazaVerEnPacientes(b.dataset.doc);
  }));
}

function trazaVerEnPacientes(q) {
  ir("pacientes");
  const inp = $("#p-buscar");
  if (inp) { inp.value = q; inp.dispatchEvent(new Event("input")); }
}

// Lo que un médico quiere saber de un paciente en diez segundos
const TRAZA_RANGOS = { "59408-5": ["SpO2", v => v < 92, "%"], "2708-6": ["SpO2", v => v < 92, "%"], "9279-1": ["FR", v => v > 22 || v < 10, "/min"],
  "8480-6": ["PAS", v => v < 90 || v > 180, "mmHg"], "8867-4": ["FC", v => v > 110 || v < 50, "lpm"],
  "8310-5": ["T", v => v >= 38.3 || v < 36, "°C"], "9269-2": ["Glasgow", v => v < 15, ""], "2339-0": ["Glucosa", v => v < 70 || v >= 250, "mg/dL"] };

async function trazaResumen(pid, remisionId, yaAbierta) {
  if (!remisionId) {
    const r = trazaUltimaRemision(await remisiones(), pid);
    if (!r) { trazaMsg("traza", `${TRAZA.paciente ? TRAZA.paciente.nombre : "Ese paciente"} no tiene remisiones en el libro.`); return; }
    remisionId = r.id; TRAZA.remision = r.id;
  }
  trazaEstado("revisando…");
  const f = await API.get(`/registro/remisiones/${remisionId}/formato`);
  const r = f.remision, p = f.paciente || {};
  const alterados = (f.signos || []).map(s => { const c = TRAZA_RANGOS[s.codigo_loinc]; return c && c[1](+s.valor) ? `${c[0]} ${String(+s.valor).replace(".", ",")} ${c[2]}`.trim() : null; }).filter(Boolean);
  const ultima = (f.bitacora || []).slice(-1)[0];
  // Fuera las marcas internas: "[URGENTE] ...", "(propuesta del asistente, ...)", "; modelo de urgencia 0.99 (critica)"
  const limpiar = (t) => String(t || "").replace(/^\[[^\]]+\]\s*/, "").replace(/\s*\(propuesta del asistente[^)]*\)/i, "").replace(/;?\s*modelo de urgencia [\d.,]+ \(\w+\)/i, "").replace(/[;,\s]+$/, "").trim();
  const partes = [
    `${primerNombre(p.nombre || r.paciente_nombre)}${p.edad != null ? `, ${p.edad} años` : ""}${p.eps ? `, ${p.eps}` : ""}.`,
    `${limpiar(f.diagnostico) || "Remisión"}${f.requiere ? `; ${limpiar(f.requiere).toLowerCase()}` : ""}. ${f.estado_texto || ESTADOS[r.estado]} hacia ${r.institucion_acepta || r.institucion_destino}${f.tiempo_texto ? ` (${f.tiempo_texto})` : ""}.`,
    (f.signos || []).length ? (alterados.length ? `Alterado: ${alterados.join(", ")}.` : "Signos sin alarmas.") : "Sin signos registrados.",
  ];
  if (p.alergias && !/ningun|sin dato|sin alergia|no refiere|niega|^no$/i.test(p.alergias)) partes.push(`Alergia: ${p.alergias}.`);
  if (ultima) partes.push(`Último contacto: ${ultima.institucion_contactada || ""}${ultima.respuesta ? ` — ${String(ultima.respuesta).slice(0, 90)}` : ""}.`);
  else if (r.abierta) partes.push("Aún no hay gestiones anotadas.");
  trazaMsg("traza", partes.join(" "), { html: yaAbierta ? "" : `<div class="traza-acciones"><button type="button" data-abrir>Abrir su ficha</button></div>` });
  const abrir = $("[data-abrir]", $("#traza-chat").lastElementChild);
  if (abrir) abrir.addEventListener("click", () => ir("ficha", { remision: remisionId }));
  trazaFijar(r.paciente_id, p.nombre || r.paciente_nombre, remisionId);
  trazaSugerir(["medico", "especialista"].includes(tzRol())
    ? ["¿Qué tan grave está?", r.abierta ? "¿A dónde lo remito?" : "¿Qué dice la guía?", r.abierta && tzRol() === "medico" ? "Dictar llamada" : null].filter(Boolean)
    : ["¿Qué tan grave está?"]);
}

function trazaListaRemisiones(lista, vacio, intro) {
  if (!lista.length) { trazaMsg("traza", vacio); return; }
  const tarjetas = lista.slice(0, 5).map(r => trazaTarjeta(r.paciente_nombre, [
    `Remisión ${r.id} · ${esc(fechaCorta(r.fecha_solicitud))}${r.extra ? ` · ${esc(r.extra)}` : ""}`,
    `<span class="traza-est est-${esc(r.estado)}">${esc(ESTADOS[r.estado])}</span> → ${esc(r.institucion_destino)}`,
  ], `data-rem="${r.id}" data-pid="${r.paciente_id}"`)).join("");
  trazaMsg("traza", intro, { html: tarjetas });
  $$(".traza-pac", $("#traza-chat").lastElementChild).forEach(b => b.addEventListener("click", () => {
    trazaFijar(+b.dataset.pid, $("strong", b).textContent, +b.dataset.rem);
    ir("ficha", { remision: +b.dataset.rem });
    trazaSugerir(["¿Cómo va?", "¿Qué tan grave está?"]);
  }));
}

async function trazaConteo(estado) {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no ve el libro de remisiones completo."); return; }
  const lista = activas(await remisiones());
  const n = (e) => lista.filter(r => r.estado === e).length;
  if (!estado) {
    trazaMsg("traza", `En el libro hay ${n("pendiente")} pendientes, ${n("en_gestion")} en gestión, ${n("aceptada")} aceptadas, ${n("rechazada")} rechazadas y ${n("completada")} completadas.`,
      { html: `<div class="traza-acciones"><button type="button" data-ir>Ver las abiertas en el libro</button></div>` });
    $("[data-ir]", $("#traza-chat").lastElementChild).addEventListener("click", () => ir("inicio", { vista: "abiertas" }));
    trazaSugerir(["Remisiones en riesgo", "Remisiones pendientes"]);
    return;
  }
  const sel = lista.filter(r => r.estado === estado).sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
  const etiqueta = { pendiente: "pendientes", en_gestion: "en gestión", aceptada: "aceptadas", rechazada: "rechazadas", completada: "completadas" }[estado];
  trazaListaRemisiones(sel, `No hay remisiones ${etiqueta}.`, `Hay ${sel.length} remisiones ${etiqueta}. Las más recientes:`);
}

async function trazaRiesgo() {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no ve el libro de remisiones."); return; }
  const hoy = Date.now();
  const abiertas = activas(await remisiones()).filter(r => ["pendiente", "en_gestion"].includes(r.estado))
    .map(r => ({ ...r, dias: Math.floor((hoy - new Date(r.fecha_solicitud + "T00:00:00").getTime()) / 864e5) }))
    .sort((a, b) => b.dias - a.dias).map(r => ({ ...r, extra: `${r.dias} día${r.dias === 1 ? "" : "s"} abierta` }));
  trazaListaRemisiones(abiertas, "No hay remisiones abiertas: nada en riesgo.", `${abiertas.length} remisiones siguen abiertas. Estas son las que más llevan esperando:`);
}

async function trazaHoy() {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no ve el libro de remisiones."); return; }
  const hoy = hoyISO();
  const lista = activas(await remisiones(true)).filter(r => String(r.fecha_solicitud).slice(0, 10) === hoy).sort((a, b) => b.id - a.id);
  trazaListaRemisiones(lista, "Hoy no se ha radicado ninguna remisión.", `Hoy se han radicado ${lista.length}:`);
}

async function trazaAlertas(clin) {
  if (!clin) { trazaMsg("traza", "Las alertas clínicas son para el equipo médico."); return; }
  const alertas = (await API.get("/alertas")).filter(a => a.estado === "activa");
  if (!alertas.length) { trazaMsg("traza", "No hay alertas críticas activas."); return; }
  const todos = await pacientes();
  const nombre = (id) => (todos.find(p => p.id === id) || {}).nombre || `Paciente ${id}`;
  const tarjetas = alertas.slice(0, 5).map(a => trazaTarjeta(nombre(a.paciente_id), [
    `<span class="traza-est est-rechazada">Crítica${a.escalada ? " · escalada" : ""}</span> · ${esc(fechaHora(a.creada_en + (/[zZ+-]\d*:?\d*$/.test(a.creada_en) ? "" : "-05:00")))}`,
    esc(String(a.mensaje).replace(/ en el paciente \d+/, "").slice(0, 70)),
  ], `data-pid="${a.paciente_id}"`)).join("");
  trazaMsg("traza", `Hay ${alertas.length} alerta${alertas.length === 1 ? "" : "s"} crítica${alertas.length === 1 ? "" : "s"} sin reconocer. Toca una para ir al paciente.`, { html: tarjetas });
  $$(".traza-pac", $("#traza-chat").lastElementChild).forEach(b => b.addEventListener("click", async () => {
    const pid = +b.dataset.pid, r = trazaUltimaRemision(await remisiones(), pid);
    trazaFijar(pid, $("strong", b).textContent, r && r.id);
    if (r) { ir("ficha", { remision: r.id }); await trazaResumen(pid, r.id, true); } else trazaMsg("traza", "Ese paciente no tiene remisión en el libro.");
  }));
}

async function trazaPendientes(clin) {
  if (!clin) { await trazaNotificaciones(); return; }
  const [p, notifs] = await Promise.all([API.get("/pendientes"), API.get("/notificaciones?leidas=false").catch(() => [])]);
  const cosas = [
    p.alertas_activas ? [`${p.alertas_activas} alerta${p.alertas_activas === 1 ? "" : "s"} crítica${p.alertas_activas === 1 ? "" : "s"}`, "Alertas"] : null,
    p.reportes_por_revisar ? [`${p.reportes_por_revisar} reporte${p.reportes_por_revisar === 1 ? "" : "s"} de IA por revisar`, "Llévame a reportes de IA"] : null,
    p.remisiones_recibidas ? [`${p.remisiones_recibidas} remisi${p.remisiones_recibidas === 1 ? "ón recibida" : "ones recibidas"} por responder`, "Llévame a interoperabilidad"] : null,
    notifs.length ? [`${notifs.length} notificaci${notifs.length === 1 ? "ón" : "ones"} sin leer`, "Llévame a notificaciones"] : null,
  ].filter(Boolean);
  if (!cosas.length) { trazaMsg("traza", "Estás al día: sin alertas, reportes ni remisiones por responder."); return; }
  trazaMsg("traza", `Tienes ${cosas.map(c => c[0]).join(", ").replace(/, ([^,]*)$/, " y $1")}. Empieza por ${p.alertas_activas ? "las alertas" : "lo primero de la lista"}.`);
  trazaSugerir(cosas.map(c => c[1]).slice(0, 3));
}

async function trazaNotificaciones() {
  const n = await API.get("/notificaciones?leidas=false");
  trazaMsg("traza", n.length ? `Tienes ${n.length} notificaci${n.length === 1 ? "ón" : "ones"} sin leer. La más reciente: ${String(n[0].mensaje).slice(0, 110)}`
    : "No tienes notificaciones sin leer.");
  if (n.length) trazaSugerir(["Llévame a notificaciones"]);
}

async function trazaConvenio(eps) {
  if (!SECCIONES.ficha.roles.includes(tzRol())) { trazaMsg("traza", "Tu rol no ve el libro de remisiones."); return; }
  const lista = activas(await remisiones()).filter(r => tzNorm(r.eps).includes(tzNorm(eps)) && r.institucion_destino !== "Hospital Universitario del Valle");
  if (!lista.length) { trazaMsg("traza", `No tengo remisiones de afiliados de «${eps}» para comparar.`); return; }
  const por = {};
  lista.forEach(r => { const g = por[r.institucion_destino] = por[r.institucion_destino] || { n: 0, conv: 0, ok: 0 }; g.n++; g.conv += r.convenio_vigente ? 1 : 0; g.ok += ["aceptada", "completada"].includes(r.estado) ? 1 : 0; });
  const top = Object.entries(por).sort((a, b) => b[1].conv - a[1].conv || b[1].ok - a[1].ok).slice(0, 3);
  trazaMsg("traza", `Para ${lista[0].eps}, según el historial: ${top.map(([n, g]) => `${n} (${g.conv} de ${g.n} con convenio, ${pct(g.ok, g.n)} % aceptadas)`).join("; ")}. Confirma el cupo por teléfono.`);
  trazaSugerir(["Llévame a instituciones"]);
}

function trazaComo(que) {
  if (que === "imagen") trazaMsg("traza", TRAZA.remision ? "En la ficha que tienes abierta, baja a «Imágenes médicas» y usa «Subir imagen» (PNG o JPEG)."
    : "Abre la ficha del paciente (dime su nombre) y en «Imágenes médicas» usa «Subir imagen».");
  else trazaMsg("traza", "En la ficha de la remisión, arriba a la derecha están las descargas: constancia en PDF, bitácora en Excel y el recurso FHIR.");
  if (!TRAZA.remision) trazaSugerir(["¿Dónde está el paciente…?"]);
}

async function trazaAccion(accion) {
  const rol = tzRol();
  const necesitaFicha = async (fn, falta) => {
    if (!TRAZA.remision) { trazaMsg("traza", "Primero dime de qué paciente: su nombre o su cédula."); return; }
    ir("ficha", { remision: TRAZA.remision });
    trazaMsg("traza", falta);
    setTimeout(fn, 900);
  };
  if (accion === "nuevo_paciente") {
    if (!["admin", "medico"].includes(rol)) { trazaMsg("traza", "Solo el médico o el administrador crean pacientes."); return; }
    ir("pacientes"); trazaMsg("traza", "Te abro el formulario de paciente nuevo.");
    setTimeout(() => typeof formularioPaciente === "function" && formularioPaciente(), 400);
  } else if (accion === "radicar") {
    if (!["admin", "medico"].includes(rol)) { trazaMsg("traza", "Solo el médico o el administrador radican remisiones."); return; }
    ir("inicio"); trazaMsg("traza", "Te abro el formulario para radicar la remisión.");
    setTimeout(() => typeof formularioRemision === "function" && formularioRemision(), 500);
  } else if (accion === "gestion") {
    await necesitaFicha(() => { const b = $("#f-anotar"); if (b) b.click(); }, "Te abro el formulario para anotar la gestión.");
  } else if (accion === "dictar") {
    await necesitaFicha(() => { const b = $("#f-dictar"); if (b) b.click(); else trazaMsg("traza", "Esta remisión ya está cerrada o tu rol no anota gestiones.", { hablar: false }); },
      "Te abro el dictado de la llamada.");
  } else if (accion === "socio") {
    await necesitaFicha(() => { const b = $("#f-socio"); if (b) b.click(); else trazaMsg("traza", "Esta remisión no se puede enviar a una institución socia.", { hablar: false }); },
      "Te abro el envío a la institución socia.");
  }
}

// ---------- Administrador: el sistema por dentro ----------
async function trazaAdmin(que) {
  if (que === "bloqueados") {
    const u = await API.get("/usuarios?solo_bloqueados=true");
    if (!u.length) { trazaMsg("traza", "No hay cuentas bloqueadas por intentos fallidos."); return; }
    trazaMsg("traza", `${u.length} cuenta${u.length === 1 ? "" : "s"} bloqueada${u.length === 1 ? "" : "s"} por intentos fallidos:`, {
      html: u.map(x => `<div class="traza-fila"><span>${esc(x.correo)}<small>${esc(ROLES[x.rol] || x.rol)}</small></span><button type="button" data-desbloquear="${x.id}">Desbloquear</button></div>`).join("") });
    $$("[data-desbloquear]", $("#traza-chat").lastElementChild).forEach(b => b.addEventListener("click", async () => {
      try { await API.post(`/usuarios/${b.dataset.desbloquear}/desbloquear`); b.textContent = "Desbloqueado"; b.disabled = true; toast("Cuenta desbloqueada.", "ok"); }
      catch (e) { toast(e.message, "error"); }
    }));
  } else if (que === "servicios") {
    const s = await API.estadoServicios();
    const N = { api: "API", postgres: "PostgreSQL", mongo: "MongoDB", pacs: "PACS", fhir: "FHIR" };
    const mal = Object.entries(s).filter(([, v]) => v !== "ok").map(([k]) => N[k] || k);
    trazaMsg("traza", mal.length ? `Atención: no responde${mal.length === 1 ? "" : "n"} ${mal.join(", ")}. Lo demás está arriba.` : "Todo arriba: API, PostgreSQL, MongoDB, PACS y FHIR responden.");
    trazaSugerir(["¿La auditoría está íntegra?", "Incidentes"]);
  } else if (que === "cadena") {
    const r = await API.get("/auditoria/verificar");
    trazaMsg("traza", r.integra ? `Íntegra: revisé los ${r.registros.toLocaleString("es-CO")} registros y la cadena de huellas cuadra.`
      : `Alterada: el registro ${r.primer_registro_alterado} no cuadra (${r.motivo}).`);
    trazaSugerir(["Llévame a seguridad"]);
  } else {
    const e = await API.get("/seguridad/estado");
    trazaMsg("traza", `${e.cifrado.todo_cifrado ? "Datos personales cifrados" : "Hay datos sin cifrar"} (${e.cifrado.documento_cifrado} de ${e.cifrado.total}); auditoría ${e.auditoria.integra ? "íntegra" : "ALTERADA"}; ${e.incidentes.ultimas_24h} incidente${e.incidentes.ultimas_24h === 1 ? "" : "s"} en 24 h y ${e.bloqueos_vigentes} bloqueo${e.bloqueos_vigentes === 1 ? "" : "s"} vigente${e.bloqueos_vigentes === 1 ? "" : "s"}.`);
    trazaSugerir(["Llévame a seguridad", "Usuarios bloqueados"]);
  }
}

// ---------- Preguntas clínicas: guías (RAG) y agente ----------
const trazaFuentes = (fuentes) => fuentes && fuentes.length
  ? `<p class="traza-fuentes">Fuentes: ${[...new Map(fuentes.map(f => [f.fuente, f])).values()].slice(0, 3).map(f => `<span title="${esc(String(f.texto || "").slice(0, 300))}">[${f.n}] ${esc(f.fuente)}</span>`).join(" ")}</p>` : "";

function trazaCorto(texto, largo = 330) {
  const limpio = String(texto || "").replace(/[*#_`]/g, "").replace(/\n{2,}/g, "\n").trim();
  if (limpio.length <= largo) return [limpio, ""];
  const corte = limpio.slice(0, largo), fin = Math.max(corte.lastIndexOf(". "), corte.lastIndexOf("\n"));
  const visible = fin > 120 ? corte.slice(0, fin + 1) : corte.slice(0, corte.lastIndexOf(" ")) + "…";
  return [visible, limpio.slice(visible.replace(/…$/, "").length).trim()];
}

function trazaMsgLargo(texto, extra) {
  const [visible, resto] = trazaCorto(texto);
  return trazaMsg("traza", visible, { html: (resto ? `<details class="traza-mas"><summary>Ver más</summary><p>${esc(resto)}</p></details>` : "") + (extra || "") });
}

async function trazaGuia(texto, clin) {
  if (!clin) { trazaMsg("traza", "Las guías clínicas son para el equipo médico."); return; }
  trazaEstado("buscando en las guías…");
  const espera = trazaMsg("traza", "Busco en las guías…", { hablar: false });
  espera.classList.add("traza-esperando");
  let r;
  try { r = await API.post("/rag/preguntar", { pregunta: texto, paciente_id: null }); } finally { espera.remove(); }
  if (r.seguridad) { trazaMsg("traza", r.respuesta); return; }
  if (!r.fuentes.length) { trazaMsg("traza", "No encontré soporte para eso en las guías del sistema, y prefiero no inventar."); return; }
  trazaMsgLargo(r.respuesta, trazaFuentes(r.fuentes));
}

async function trazaPreguntar(texto, clin) {
  if (!clin) {
    trazaMsg("traza", "No entendí. Te llevo a cualquier sección de tu menú: dime, por ejemplo, «llévame a notificaciones».");
    return;
  }
  trazaEstado("pensando…");
  const espera = trazaMsg("traza", "Lo reviso…", { hablar: false });
  espera.classList.add("traza-esperando");
  let r;
  if (TRAZA.propuesta && TRAZA.propuesta.conv === TRAZA.conversacion)       // la propuesta espera en su hilo;
    TRAZA.conversacion = "traza-" + Date.now().toString(36);                 // la pregunta nueva va en otro
  try {
    r = await API.post("/agente/chat", { mensaje: texto, conversacion_id: TRAZA.conversacion, paciente_id: TRAZA.paciente ? TRAZA.paciente.id : null });
  } finally { espera.remove(); }
  if (r.estado === "respondido") {
    // Al médico: la respuesta y sus fuentes. Al admin, además, la traza técnica completa.
    let extra = trazaFuentes(r.fuentes);
    if (tzRol() === "admin" && (r.pasos || []).length) {
      const llamadas = r.pasos.filter(p => p.tipo === "llamada"), res = r.pasos.filter(p => p.tipo === "resultado");
      extra += `<details class="traza-mas traza-tecnico"><summary>Detalle técnico (${llamadas.length} herramienta${llamadas.length === 1 ? "" : "s"} · modelo ${esc(r.modelo || "")})</summary>
        ${llamadas.map(l => { const x = res.find(y => y.herramienta === l.herramienta); return `<p><code>${esc(l.herramienta)}</code> ${esc(JSON.stringify(l.args || {}))}<br>${esc(x ? x.texto.slice(0, 260) : "")}</p>`; }).join("")}
        <button type="button" data-llm>Ver lo enviado al LLM (${(r.invocaciones || []).length})</button></details>`;
    }
    const m = trazaMsgLargo(r.respuesta || "No tengo una respuesta para eso.", extra);
    const llm = $("[data-llm]", m);
    if (llm) llm.addEventListener("click", () => ir("seguridad", { pestana: "llm" }));
    if (!TRAZA.paciente && /abre (primero )?un paciente/i.test(r.respuesta || "")) trazaSugerir(["¿Dónde está el paciente…?"]);
    else if (/la mejor opci[oó]n es/i.test(r.respuesta || "") && tzRol() === "medico") trazaSugerir(["Remite este paciente", "¿Qué tan grave está?"]);
    else if (TRAZA.paciente && /probabilidad de urgencia/i.test(r.respuesta || "")) trazaSugerir(["¿A dónde lo remito?", "¿Qué dice la guía?"]);
  } else if (r.estado === "pendiente_aprobacion") {
    trazaPropuesta(r, TRAZA.conversacion);
  } else trazaMsg("traza", r.detalle || "No pude responder ahora. Intenta de nuevo.");
}

// ---------- Propuestas del agente: se deciden AQUÍ mismo ----------
// El médico aprueba o rechaza con un botón o con palabras ("sí, radícala", "no").
// Una propuesta sin decidir ya no bloquea a Traza: las preguntas nuevas siguen en
// otra conversación y la propuesta queda esperando con sus botones.
function trazaPropuesta(r, conv) {
  const a = (r.acciones || [])[0] || { args: {} };
  const x = a.args || {};
  TRAZA.propuesta = { conv, destino: x.institucion_destino || "la institución sugerida" };
  const medico = tzRol() === "medico";
  trazaMsg("traza", `${r.detalle ? "Tienes pendiente esta propuesta" : "Te propongo"}: remitir${TRAZA.paciente ? " a " + primerNombre(TRAZA.paciente.nombre) : ""} a ${TRAZA.propuesta.destino}${x.prioridad ? `, prioridad ${({ critica: "crítica" })[x.prioridad] || x.prioridad}` : ""}. ${medico ? "¿La radico? Nada se crea hasta que digas que sí." : "Solo el médico puede aprobarla."}`, {
    html: `${x.motivo ? `<p class="traza-nota">Motivo: ${esc(String(x.motivo).slice(0, 160))}</p>` : ""}
      <div class="traza-acciones">${medico ? `<button type="button" data-si>Sí, radicar</button> <button type="button" data-no>No, descartar</button>` : ""}</div>` });
  const caja = $("#traza-chat").lastElementChild;
  const si = $("[data-si]", caja), no = $("[data-no]", caja);
  if (si) si.addEventListener("click", () => { trazaEnviar("Sí, radícala", false); });
  if (no) no.addEventListener("click", () => { trazaEnviar("No, descártala", false); });
  if (medico) trazaSugerir(["Sí, radícala", "No, descártala"]);
}

async function trazaDecidir(aprobar, motivo) {
  const p = TRAZA.propuesta;
  if (!p) { trazaMsg("traza", "No tengo ninguna propuesta esperando tu decisión."); return; }
  if (tzRol() !== "medico") { trazaMsg("traza", "Solo el médico puede aprobar o rechazar una remisión."); return; }
  trazaEstado(aprobar ? "radicando…" : "descartando…");
  const r = await API.post("/agente/decidir", { conversacion_id: p.conv, decision: aprobar ? "approve" : "reject",
    motivo: aprobar ? null : (motivo || "Descartada por el médico desde Traza") });
  TRAZA.propuesta = null;
  $$("#traza-chat [data-si], #traza-chat [data-no]").forEach(b => { b.disabled = true; });
  if (r.estado === "sin_pendientes") { trazaMsg("traza", "Esa propuesta ya estaba decidida, así que sigo contigo. ¿Qué necesitas?"); return; }
  if (aprobar) {
    remisionesCache = null;
    trazaMsg("traza", String(r.respuesta || "Listo: quedó radicada en el libro como pendiente.").replace(/^\[modelo simulado\]\s*/, ""),
      { html: `<div class="traza-acciones"><button type="button" data-libro>Ver en el libro</button></div>` });
    $("[data-libro]", $("#traza-chat").lastElementChild).addEventListener("click", () => ir("inicio", { vista: "abiertas" }));
    trazaSugerir(["Dictar llamada", "¿Qué tengo pendiente?"]);
  } else {
    trazaMsg("traza", "Descartada: no se radicó nada. Quedó registrada como rechazada por ti. ¿Qué conducta prefieres?");
  }
}

async function trazaRevisarPropuesta() {
  const p = TRAZA.propuesta;
  if (!p) { trazaMsg("traza", "Perfecto, no hay nada pendiente. ¿Qué sigue?"); return; }
  const h = await API.get(`/agente/historial?conversacion_id=${encodeURIComponent(p.conv)}`).catch(() => ({ pendiente: true }));
  if (!h.pendiente) {
    TRAZA.propuesta = null;
    remisionesCache = null;
    trazaMsg("traza", "Confirmado: la propuesta ya está decidida. Sigo contigo, ¿qué necesitas?");
  } else {
    trazaMsg("traza", `Todavía la veo sin decidir (remisión a ${p.destino}). ¿La radico?`, {
      html: `<div class="traza-acciones"><button type="button" data-si>Sí, radicar</button> <button type="button" data-no>No, descartar</button></div>` });
    const caja = $("#traza-chat").lastElementChild;
    $("[data-si]", caja).addEventListener("click", () => trazaEnviar("Sí, radícala", false));
    $("[data-no]", caja).addEventListener("click", () => trazaEnviar("No, descártala", false));
  }
}

// Sugerencias de qué preguntar después (aparecen como botones bajo la conversación)
function trazaSugerir(lista) {
  const caja = $("#traza-chips");
  if (!caja) return;
  caja.classList.remove("oculto");
  caja.innerHTML = lista.map(s => `<button type="button">${esc(s)}</button>`).join("");
  $$("button", caja).forEach(b => b.addEventListener("click", () => {
    if (b.textContent.endsWith("…?")) { $("#traza-texto").value = "¿Dónde está el paciente "; $("#traza-texto").focus(); return; }
    trazaEnviar(b.textContent, false);
  }));
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

// Cuando el usuario abre una ficha por su cuenta, Traza se entera: "este paciente"
// pasa a ser ese. (Mismo recurso que eventos.js: envolver la función global ir.)
if (typeof ir === "function") {
  const irOriginal = ir;
  ir = function (seccion, params = {}) {          // eslint-disable-line no-func-assign
    irOriginal(seccion, params);
    try {
      if (seccion === "ficha" && params.remision && TRAZA.remision !== +params.remision && TRAZA_CLINICOS.includes(tzRol())) {
        remisiones().then(lista => {
          const r = lista.find(x => x.id === +params.remision);
          if (r) trazaFijar(r.paciente_id, r.paciente_nombre, r.id);
        }).catch(() => {});
      }
    } catch { /* Traza nunca rompe la navegación */ }
  };
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
