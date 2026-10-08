// ==========================================================================
// TrazaRed HUV — lógica de la interfaz (SPA: una sola página, varias vistas)
// ==========================================================================
// Flujo: login -> /me (quién soy y mi rol) -> menú lateral según el rol ->
// cada sección se dibuja en <main>. La API es la que protege los datos (JWT +
// roles); la interfaz solo evita mostrar lo que ese rol no puede usar.
"use strict";

// ---------------------------------------------------------------- utilidades
const $ = (sel, raiz = document) => raiz.querySelector(sel);
const $$ = (sel, raiz = document) => [...raiz.querySelectorAll(sel)];

// Escapa texto antes de meterlo en HTML (defensa contra inyección de HTML/JS)
function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const ZONA = "America/Bogota";
const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];

function fechaCorta(iso) {                       // "2026-09-07" -> "7 sep 2026" (sin correr el día)
  if (!iso) return "—";
  const [a, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  return `${d} ${MESES[m - 1]} ${a}`;
}
function fechaHora(iso) {                        // instante con zona -> hora de Colombia
  if (!iso) return "—";
  const f = new Date(iso);
  if (isNaN(f)) return esc(iso);
  return f.toLocaleString("es-CO", { timeZone: ZONA, day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}
function fechaDicom(v) { return v && v.length === 8 ? fechaCorta(`${v.slice(0, 4)}-${v.slice(4, 6)}-${v.slice(6, 8)}`) : "—"; }
function diasDesde(iso) {
  const [a, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  const hoy = new Date();
  return Math.round((Date.UTC(hoy.getFullYear(), hoy.getMonth(), hoy.getDate()) - Date.UTC(a, m - 1, d)) / 86400000);
}
function edad(iso) {
  if (!iso) return null;
  const [a, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  const hoy = new Date();
  let e = hoy.getFullYear() - a;
  if (hoy.getMonth() + 1 < m || (hoy.getMonth() + 1 === m && hoy.getDate() < d)) e--;
  return e;
}
const hoyISO = () => new Date().toLocaleDateString("en-CA", { timeZone: ZONA });
const iniciales = n => String(n || "?").trim().split(/\s+/).slice(0, 2).map(p => p[0]).join("").toUpperCase();
const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
const primerNombre = n => String(n || "").trim().split(/\s+/)[0] || "";

function toast(msg, tipo = "info") {
  const t = document.createElement("div");
  t.className = `toast ${tipo}`;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
const cargando = (txt = "Cargando…") => `<div class="cargando"><div class="spinner"></div>${esc(txt)}</div>`;
function debounce(fn, ms = 300) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// Íconos de línea (trazos SVG) usados en el menú, tarjetas y estados
const ICONOS = {
  inicio: '<path d="M3 11 12 4l9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  remisiones: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/>',
  pacientes: '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5M16 4.5a3.5 3.5 0 0 1 0 7M18 14.8c1.8.7 3 2.5 3.5 5.2"/>',
  hospitales: '<path d="M4 21V8h5V4h6v4h5v13M2 21h20M11 8h2M12 7v2M7 12h2M7 16h2M15 12h2M15 16h2M11 21v-3h2v3"/>',
  reportes: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  usuarios: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  auditoria: '<path d="M12 3 4 6v6c0 4.5 3.4 8.3 8 9 4.6-.7 8-4.5 8-9V6z"/><path d="m9 12 2 2 4-4"/>',
  reloj: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  check: '<path d="m5 12 5 5 9-10"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  flecha: '<path d="M12 5v14M6 13l6 6 6-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
  persona: '<circle cx="12" cy="8" r="4"/><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6"/>',
  buscar: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
  candado: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  alerta: '<path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18v.5"/>',
  imagen: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/>',
  trazo: '<path d="M3 12h4l3 7 4-14 3 7h4"/>',
};
const icono = (n, extra = "") => `<svg viewBox="0 0 24 24" ${extra} fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${ICONOS[n] || ""}</svg>`;

// ---------------------------------------------------------------- catálogos
const ESTADOS = { pendiente: "Pendiente", en_gestion: "En gestión", aceptada: "Aceptada", rechazada: "Rechazada", completada: "Completada" };
const PILDORA = {
  pendiente: ["reloj", "Remisión pendiente"], en_gestion: ["reloj", "Remisión en proceso"],
  aceptada: ["check", "Remisión aceptada"], rechazada: ["x", "Remisión rechazada"], completada: ["check", "Traslado completado"],
};
const pildora = (e, grande = false) => {
  const [ic, txt] = PILDORA[e] || ["info", e];
  return `<span class="pildora ${grande ? "pildora-grande" : ""} est-${esc(e)}">${icono(ic)}${esc(grande ? txt : ESTADOS[e] || e)}</span>`;
};
const EXPLICA_PACIENTE = {
  pendiente: "Tu remisión fue solicitada y está esperando que el hospital empiece a gestionarla.",
  en_gestion: "El hospital está contactando instituciones para tu traslado.",
  aceptada: "Una institución aceptó recibirte. Pronto se programa tu traslado.",
  rechazada: "La institución no pudo recibirte. El hospital buscará otra opción.",
  completada: "Tu traslado ya se realizó.",
};
const ROLES = { admin: "Administrador", medico: "Médico", especialista: "Especialista", contable: "Contable", eps: "EPS", paciente: "Paciente" };

const VITALES = [
  { loinc: "8867-4", tipo: "Frecuencia cardiaca", unidad: "/min", min: 60, max: 100 },
  { loinc: "8480-6", tipo: "Presión arterial sistólica", unidad: "mm[Hg]", min: 90, max: 139 },
  { loinc: "8462-4", tipo: "Presión arterial diastólica", unidad: "mm[Hg]", min: 60, max: 89 },
  { loinc: "9279-1", tipo: "Frecuencia respiratoria", unidad: "/min", min: 12, max: 20 },
  { loinc: "59408-5", tipo: "Saturación de oxígeno", unidad: "%", min: 92, max: 100 },
  { loinc: "8310-5", tipo: "Temperatura corporal", unidad: "Cel", min: 36, max: 37.9 },
  { loinc: "9269-2", tipo: "Escala de Glasgow", unidad: "{score}", min: 15, max: 15 },
];
const VITAL = Object.fromEntries(VITALES.map(v => [v.loinc, v]));
const unidadBonita = u => String(u || "").replace("mm[Hg]", "mmHg").replace("Cel", "°C").replace("{score}", "/15");

// Instituciones de la red (ciudad para la tarjeta del hospital)
const CIUDADES = {
  "Hospital Universitario San José (Popayán)": "Popayán, Cauca",
  "Hospital Francisco de Paula Santander (Santander de Quilichao)": "Santander de Quilichao, Cauca",
  "Hospital Local de Villa Rica": "Villa Rica, Cauca",
  "ESE Norte 2 (Caloto)": "Caloto, Cauca",
  "Hospital Departamental Tomás Uribe Uribe (Tuluá)": "Tuluá, Valle del Cauca",
  "Hospital San Rafael (Buga)": "Buga, Valle del Cauca",
  "Hospital Raúl Orejuela Bueno (Palmira)": "Palmira, Valle del Cauca",
  "Hospital Departamental de Buenaventura": "Buenaventura, Valle del Cauca",
};
const ciudadDe = inst => CIUDADES[inst] || "Cali, Valle del Cauca";
const INSTITUCIONES = [
  "Hospital Universitario del Valle", "Fundación Valle del Lili", "Clínica Imbanaco", "Clínica de Occidente",
  "Clínica Nuestra Señora de los Remedios", "Clínica Farallones", "Hospital San Juan de Dios Cali",
  "Clínica Colombia Cali", "Clínica Versalles", "Clínica Sebastián de Belalcázar",
  "Hospital Universitario San José (Popayán)", ...Object.keys(CIUDADES).filter(k => !k.includes("San José")),
];
const EPS = ["Emssanar", "Coosalud", "Nueva EPS", "Asmet Salud", "SOS", "Sanitas", "Sura", "Salud Total"];

// "Diagnóstico; requiere X" -> ["Diagnóstico", "Requiere X"]
function partirMotivo(m) {
  const [d, ...r] = String(m || "").split(";");
  const resto = r.join(";").trim();
  return [d.trim(), resto ? resto[0].toUpperCase() + resto.slice(1) : "—"];
}

// ---------------------------------------------------------------- estado
const sesion = { usuario: null, reloj: null, seccion: null };
let remisionesCache = null;         // remisiones visibles para este usuario (se recarga al cambiar algo)
let pacientesCache = null;
const imagenes = { urls: new Map(), version: 0 };

async function remisiones(forzar = false) {
  if (!remisionesCache || forzar) {
    const params = sesion.usuario.rol === "admin" ? "?incluir_inactivas=true" : "";
    remisionesCache = await API.get(`/remisiones${params}`);
  }
  return remisionesCache;
}
const activas = lista => lista.filter(r => r.activo !== false);
async function pacientes() {
  if (!pacientesCache) pacientesCache = await API.get("/pacientes");
  return pacientesCache;
}
function liberarImagenes() {
  imagenes.version++;
  for (const url of imagenes.urls.values()) URL.revokeObjectURL(url);
  imagenes.urls.clear();
}

// ==========================================================================
// LOGIN
// ==========================================================================
try { const u = localStorage.getItem("trazared_usuario"); if (u) { $("#login-usuario").value = u; $("#recordar").checked = true; } } catch { /* sin storage */ }

$("#toggle-clave").addEventListener("click", () => {
  const c = $("#login-clave");
  const ver = c.type === "password";
  c.type = ver ? "text" : "password";
  $("#toggle-clave").classList.toggle("activo", ver);
  $("#toggle-clave").setAttribute("aria-label", ver ? "Ocultar contraseña" : "Mostrar contraseña");
});
$("#olvide").addEventListener("click", (e) => {
  e.preventDefault();
  mensajeLogin("info", "¿Olvidaste tu contraseña?", "Por seguridad, las contraseñas solo las restablece el administrador (Oficina de Referencia y Contrarreferencia del HUV).");
});

function mensajeLogin(tipo, titulo, texto = "", extra = "") {
  const ic = { error: "candado", alerta: "alerta", info: "info" }[tipo];
  $("#login-mensaje").innerHTML = titulo
    ? `<div class="aviso aviso-${tipo}">${icono(ic)}<div><strong>${esc(titulo)}</strong>${esc(texto)}${extra}</div></div>` : "";
}

$("#form-login").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  const usuario = $("#login-usuario").value.trim();
  const clave = $("#login-clave").value;
  const tiembla = () => { const f = $("#form-login"); f.classList.remove("tiembla"); void f.offsetWidth; f.classList.add("tiembla"); };
  if (!usuario || !clave) { mensajeLogin("alerta", "Faltan datos", "Escribe tu usuario y tu contraseña."); tiembla(); return; }
  const btn = $("#btn-entrar");
  btn.disabled = true; btn.textContent = "Verificando…";
  try {
    await API.login(usuario, clave);
    try { $("#recordar").checked ? localStorage.setItem("trazared_usuario", usuario) : localStorage.removeItem("trazared_usuario"); } catch { /* sin storage */ }
    mensajeLogin();
    $("#login-clave").value = "";
    await entrar();
  } catch (e) {
    const d = e.detalle || {};
    if (e.status === 423) {
      mensajeLogin("error", "Usuario bloqueado", "Superaste los 3 intentos permitidos. Solo un administrador puede desbloquear tu cuenta.");
    } else if (e.status === 401 && typeof d.intentos_restantes === "number") {
      const q = d.intentos_restantes;
      const barras = [1, 2, 3].map(i => `<span class="${i <= 3 - q ? "usado" : ""}"></span>`).join("");
      mensajeLogin("alerta", "Contraseña incorrecta", `Te ${q === 1 ? "queda 1 intento" : `quedan ${q} intentos`} antes de que la cuenta se bloquee.`, `<div class="intentos" aria-hidden="true">${barras}</div>`);
    } else if (e.status === 0) {
      mensajeLogin("error", "Sin conexión", e.message);
    } else {
      mensajeLogin("error", "No pudiste entrar", e.message || "Correo o contraseña incorrectos");
    }
    tiembla();
    $("#login-clave").value = "";
    $("#login-clave").focus();
  } finally {
    btn.disabled = false; btn.textContent = "Iniciar sesión";
  }
});

async function pintarServicios() {
  const est = await API.estadoServicios();
  $$("#servicios .punto").forEach(p => {
    const s = p.dataset.servicio;
    p.classList.toggle("ok", est[s] === "ok");
    p.classList.toggle("error", est[s] !== "ok");
    p.parentElement.title = `${s}: ${est[s] === "ok" ? "conectado" : "sin conexión"}`;
  });
}

// ==========================================================================
// SESIÓN
// ==========================================================================
async function entrar() {
  try { sesion.usuario = await API.get("/me"); } catch { API.salir(); return; }
  const u = sesion.usuario;
  remisionesCache = null; pacientesCache = null;
  $("#vista-login").classList.add("oculto");
  $("#vista-app").classList.remove("oculto");
  $("#avatar").textContent = iniciales(u.nombre);
  $("#quien-nombre").textContent = u.nombre;
  $("#quien-rol").textContent = u.rol === "eps" ? `EPS · ${u.eps_nombre || ""}` : ROLES[u.rol];
  armarMenu();
  iniciarReloj();
  ir("inicio");
  actualizarNotificaciones();
}

function salir(motivo) {
  API.salir();
  clearInterval(sesion.reloj);
  sesion.usuario = null;
  liberarImagenes();
  if ($("#visor").open) $("#visor").close();
  cerrarCapas();
  $("#vista-app").classList.add("oculto");
  $("#vista-login").classList.remove("oculto");
  $("#contenido").innerHTML = "";
  if (motivo) mensajeLogin("info", "Sesión cerrada", motivo);
  pintarServicios();
  $("#login-usuario").focus();
}
$("#btn-salir").addEventListener("click", () => salir());
$("#btn-salir-menu").addEventListener("click", () => salir());

API.alExpirar = (detalle) => {
  const texto = typeof detalle === "string" && /bloquead/i.test(detalle) ? "Tu usuario fue bloqueado." : "Tu sesión ya no es válida (el token venció o fue alterado).";
  salir(`${texto} Vuelve a iniciar sesión.`);
};

function iniciarReloj() {             // cuenta regresiva según el "exp" del JWT (60 min)
  clearInterval(sesion.reloj);
  const pintar = () => {
    const d = API.datosToken();
    if (!d || !d.exp) return;
    const s = Math.floor(d.exp - Date.now() / 1000);
    if (s <= 0) { salir("Tu sesión de 60 minutos terminó por seguridad."); return; }
    const r = $("#reloj");
    r.textContent = `Tu sesión vence en ${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    r.classList.toggle("pronto", s < 300);
  };
  pintar();
  sesion.reloj = setInterval(pintar, 1000);
}

// ---------------------------------------------------------------- barra superior
function alternar(panel, boton) {
  const abierto = !panel.classList.contains("oculto");
  $$(".menu-flotante").forEach(p => p.classList.add("oculto"));
  if (!abierto) panel.classList.remove("oculto");
  boton.setAttribute("aria-expanded", String(!abierto));
}
$("#btn-usuario").addEventListener("click", (e) => { e.stopPropagation(); alternar($("#panel-usuario"), $("#btn-usuario")); });
$("#btn-campana").addEventListener("click", (e) => { e.stopPropagation(); alternar($("#panel-notif"), $("#btn-campana")); });
document.addEventListener("click", (e) => { if (!e.target.closest(".desplegable")) $$(".menu-flotante").forEach(p => p.classList.add("oculto")); });
$("#marca-inicio").addEventListener("click", (e) => { e.preventDefault(); ir("inicio"); });
$("#btn-menu").addEventListener("click", () => $("#lateral").classList.toggle("abierto"));

async function actualizarNotificaciones() {
  if (!sesion.usuario || sesion.usuario.rol === "contable") return;
  try {
    const lista = activas(await remisiones()).filter(r => r.estado === "pendiente" || r.estado === "en_gestion")
      .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
    const ins = $("#insignia");
    ins.textContent = lista.length > 99 ? "99+" : lista.length;
    ins.classList.toggle("oculto", !lista.length);
    const titulo = sesion.usuario.rol === "paciente" ? "Tus remisiones en curso" : "Remisiones que esperan respuesta";
    $("#panel-notif").innerHTML = `<h4>${titulo}</h4>` + (lista.length
      ? lista.slice(0, 6).map(r => `<button class="menu-item" data-rem="${r.id}">${esc(r.paciente_nombre || r.institucion_destino)} ${pildora(r.estado)}
          <small>${esc(r.institucion_destino)} · ${fechaCorta(r.fecha_solicitud)}</small></button>`).join("")
      : `<p class="tenue" style="padding:.4rem .7rem">No hay remisiones en espera.</p>`);
    $$("#panel-notif [data-rem]").forEach(b => b.addEventListener("click", () => { $("#panel-notif").classList.add("oculto"); ir("ficha", { remision: +b.dataset.rem }); }));
  } catch { /* la campana es informativa: si falla no bloquea nada */ }
}

// ==========================================================================
// MENÚ LATERAL Y NAVEGACIÓN
// ==========================================================================
const SECCIONES = {
  inicio: { titulo: "Inicio", icono: "inicio", roles: ["admin", "medico", "especialista", "contable", "eps", "paciente"], pintar: vistaInicio },
  remisiones: { titulo: "Remisiones", icono: "remisiones", roles: ["admin", "medico", "especialista", "eps"], pintar: vistaRemisiones },
  pacientes: { titulo: "Pacientes", icono: "pacientes", roles: ["admin", "medico", "especialista"], pintar: vistaPacientes },
  hospitales: { titulo: "Hospitales de destino", icono: "hospitales", roles: ["admin", "medico", "especialista", "eps"], pintar: vistaHospitales },
  reportes: { titulo: "Reportes", icono: "reportes", roles: ["admin", "medico", "especialista", "eps"], pintar: vistaReportes },
  usuarios: { titulo: "Usuarios", icono: "usuarios", roles: ["admin"], pintar: vistaUsuarios, grupo: "Administración" },
  auditoria: { titulo: "Auditoría", icono: "auditoria", roles: ["admin"], pintar: vistaAuditoria, grupo: "Administración" },
  ficha: { oculta: true, roles: ["admin", "medico", "especialista", "eps", "paciente"], pintar: vistaFicha, padre: "remisiones" },
};

function armarMenu() {
  const rol = sesion.usuario.rol;
  let grupo = null;
  $("#menu").innerHTML = Object.entries(SECCIONES).filter(([, s]) => !s.oculta && s.roles.includes(rol)).map(([id, s]) => {
    let pre = "";
    if (s.grupo && s.grupo !== grupo) { grupo = s.grupo; pre = `<li class="separador"></li><li class="etiqueta-menu">${esc(grupo)}</li>`; }
    const titulo = id === "inicio" && rol === "paciente" ? "Mi remisión" : id === "remisiones" && rol === "medico" ? "Mis remisiones" : s.titulo;
    return `${pre}<li><a href="#" data-seccion="${id}">${icono(s.icono)}${esc(titulo)}</a></li>`;
  }).join("");
  $$("#menu a").forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); ir(a.dataset.seccion); }));
}

function ir(seccion, params = {}) {
  sesion.seccion = seccion;
  liberarImagenes();
  cerrarCapas();
  $("#lateral").classList.remove("abierto");
  const marcar = SECCIONES[seccion].padre && seccion === "ficha" ? (params.desde || "remisiones") : seccion;
  $$("#menu a").forEach(a => a.classList.toggle("activo", a.dataset.seccion === marcar));
  window.scrollTo(0, 0);
  SECCIONES[seccion].pintar(params);
}

// ==========================================================================
// MODALES
// ==========================================================================
function cerrarCapas() { $$(".fondo-modal").forEach(m => m.remove()); }
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { const c = $$(".fondo-modal"); if (c.length) c[c.length - 1].remove(); } });

function abrirFormulario({ titulo, campos, textoBoton = "Guardar", alGuardar, alAbrir }) {
  const fondo = document.createElement("div");
  fondo.className = "fondo-modal";
  fondo.innerHTML = `<form class="modal" novalidate>
      <div class="modal-cabeza"><h3>${esc(titulo)}</h3><button type="button" class="btn-icono" data-cerrar aria-label="Cerrar">${icono("x")}</button></div>
      <div class="modal-cuerpo"><p class="error-form oculto"></p>${campos}</div>
      <div class="modal-pie"><button type="button" class="btn btn-secundario" data-cerrar>Cancelar</button>
        <button type="submit" class="btn btn-primario">${esc(textoBoton)}</button></div></form>`;
  document.body.appendChild(fondo);
  const form = $("form", fondo);
  fondo.addEventListener("mousedown", (e) => { if (e.target === fondo) fondo.remove(); });
  $$("[data-cerrar]", fondo).forEach(b => b.addEventListener("click", () => fondo.remove()));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $(".error-form", form), btn = $("button[type=submit]", form);
    err.classList.add("oculto"); btn.disabled = true;
    try {
      const d = Object.fromEntries(new FormData(form));
      $$("input[type=checkbox]", form).forEach(c => { d[c.name] = c.checked; });
      await alGuardar(d, form);
      fondo.remove();
    } catch (x) { err.textContent = x.message; err.classList.remove("oculto"); } finally { btn.disabled = false; }
  });
  if (alAbrir) alAbrir(form);
  const primero = $("input, select, textarea", form);
  if (primero) primero.focus();
}

// ==========================================================================
// VISTA: INICIO
// ==========================================================================
async function vistaInicio() {
  const u = sesion.usuario;
  const main = $("#contenido");
  const saludo = {
    admin: "Aquí tienes el panorama de las remisiones del hospital, los usuarios y la auditoría.",
    medico: "Aquí puedes gestionar las remisiones de tus pacientes y hacer seguimiento a su estado.",
    eps: "Aquí puedes hacer seguimiento a las remisiones de tus afiliados.",
    paciente: "Aquí puedes ver a dónde vas a ser remitido y en qué va tu traslado.",
    especialista: "Aquí puedes ver las remisiones y atender a los pacientes que te remiten.",
    contable: "Aquí vas a gestionar la facturación de los servicios. Por tu rol no ves datos clínicos.",
  }[u.rol];
  main.innerHTML = `<div class="pagina">
    <section class="tarjeta bienvenida">
      <div><h2>Hola, ${esc(u.rol === "paciente" ? primerNombre(u.nombre) : u.nombre)}</h2><p>${saludo}</p></div>
      <div class="bienvenida-arte" aria-hidden="true"><svg><use href="#hospital"/></svg><span class="flecha"></span><svg><use href="#hospital"/></svg></div>
    </section>
    <div id="inicio-kpis"></div>
    <div id="inicio-cuerpo">${cargando()}</div></div>`;
  if (u.rol === "contable") {   // el contable no ve remisiones ni datos clínicos
    $("#inicio-cuerpo").innerHTML = `<div class="tarjeta vacio">Aquí irá la facturación (contabilidad).</div>`;
    return;
  }
  let lista;
  try { lista = activas(await remisiones(true)); } catch (e) { $("#inicio-cuerpo").innerHTML = `<div class="tarjeta vacio">${esc(e.message)}</div>`; return; }
  actualizarNotificaciones();

  if (u.rol !== "paciente") {
    const c = k => lista.filter(r => r.estado === k).length;
    const exito = c("aceptada") + c("completada");
    $("#inicio-kpis").innerHTML = `<div class="kpis">
      ${kpi("remisiones", "tono-azul", lista.length, "Remisiones activas")}
      ${kpi("reloj", "tono-ambar", c("pendiente") + c("en_gestion"), "Pendientes y en gestión")}
      ${kpi("check", "tono-verde", exito, "Aceptadas o completadas")}
      ${kpi("x", "tono-rojo", c("rechazada"), "Rechazadas")}
      ${kpi("trazo", "tono-violeta", `${pct(exito, lista.length)}%`, "Tasa de éxito")}</div>`;
  }
  if (!lista.length) { $("#inicio-cuerpo").innerHTML = `<div class="tarjeta vacio">No hay remisiones registradas.</div>`; return; }

  // Remisiones que requieren atención (las más recientes pendientes / en gestión)
  const atencion = lista.filter(r => r.estado === "pendiente" || r.estado === "en_gestion")
    .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
  const base = u.rol === "paciente" ? [...lista].sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud))) : atencion;
  const elegida = (base[0] || lista[0]).id;
  const lateral = (u.rol === "paciente" ? base : atencion).slice(0, 8);

  $("#inicio-cuerpo").innerHTML = `<h3 class="seccion-titulo" style="margin-bottom:.9rem">Información del paciente</h3>
    <div class="inicio-grid"><div id="inicio-ficha"></div>
      ${lateral.length > 1 || u.rol !== "paciente" ? `<aside class="tarjeta"><div class="tarjeta-cuerpo" style="padding-bottom:.2rem">
        <h4 style="color:var(--navy)">${u.rol === "paciente" ? "Tus remisiones" : "Requieren atención"}</h4>
        <p class="tenue">${u.rol === "paciente" ? "Toca una para ver su estado." : "Pendientes o en gestión, las más recientes primero."}</p></div>
        <ul class="lista" id="lista-atencion">${lateral.map(r => `<li><button data-rem="${r.id}">
          <span class="avatar" style="width:34px;height:34px;font-size:.72rem">${esc(iniciales(r.paciente_nombre || "R"))}</span>
          <span class="txt"><strong>${esc(r.paciente_nombre || r.institucion_destino)}</strong>
            <small><b style="color:${TEXTO_ESTADO[r.estado]}">${esc(ESTADOS[r.estado])}</b> · ${esc(r.institucion_destino)}</small>
            <small>${fechaCorta(r.fecha_solicitud)}</small></span>
          <span class="estado-punto" style="background:${COLOR_ESTADO[r.estado]}" aria-hidden="true"></span></button></li>`).join("") || `<li class="vacio">Nada pendiente</li>`}</ul>
        ${u.rol !== "paciente" ? `<div style="padding:0 1rem 1rem"><button class="btn btn-secundario btn-bloque" id="ver-todas">Ver todas las remisiones</button></div>` : ""}
      </aside>` : ""}</div>`;
  const abrir = async (id) => {
    $$("#lista-atencion button").forEach(b => b.classList.toggle("activo", +b.dataset.rem === id));
    liberarImagenes();
    await pintarFicha($("#inicio-ficha"), { remisionId: id });
  };
  $$("#lista-atencion button").forEach(b => b.addEventListener("click", () => abrir(+b.dataset.rem)));
  if ($("#ver-todas")) $("#ver-todas").addEventListener("click", () => ir("remisiones"));
  abrir(elegida);
}
const kpi = (ic, tono, valor, etiqueta) => `<div class="tarjeta kpi"><span class="kpi-icono ${tono}">${icono(ic)}</span>
  <div><div class="valor">${esc(valor)}</div><div class="etiqueta">${esc(etiqueta)}</div></div></div>`;

// ==========================================================================
// FICHA DEL PACIENTE / REMISIÓN (componente del mockup)
// ==========================================================================
async function vistaFicha({ remision, paciente, desde }) {
  $("#contenido").innerHTML = `<div class="pagina">
    <button class="volver" id="volver">← Volver</button><div id="ficha"></div></div>`;
  $("#volver").addEventListener("click", () => ir(desde || (sesion.usuario.rol === "paciente" ? "inicio" : "remisiones")));
  await pintarFicha($("#ficha"), { remisionId: remision, pacienteId: paciente });
}

async function pintarFicha(caja, { remisionId, pacienteId }) {
  const u = sesion.usuario;
  const clinico = u.rol === "admin" || u.rol === "medico" || u.rol === "especialista";
  caja.innerHTML = `<div class="tarjeta">${cargando("Cargando ficha…")}</div>`;
  let r = null, p = null, obs = [], contactos = [];
  try {
    if (remisionId) r = await API.get(`/remisiones/${remisionId}`);
    const pid = pacienteId || (r && r.paciente_id) || (u.rol === "paciente" ? u.paciente_id : null);
    if (pid) p = await API.get(`/pacientes/${pid}`);
    if (r && clinico) {
      [obs, contactos] = await Promise.all([
        API.get(`/observaciones?remision_id=${r.id}`),
        API.get(`/gestiones-contacto?remision_id=${r.id}`).then(ds => ds.flatMap(d => d.contactos || []))
          .then(cs => cs.sort((a, b) => String(a.fecha).localeCompare(String(b.fecha)))).catch(() => []),
      ]);
    }
  } catch (e) { caja.innerHTML = `<div class="tarjeta vacio">${esc(e.message)}</div>`; return; }
  if (!document.body.contains(caja)) return;

  const nombre = (p && p.nombre) || (r && r.paciente_nombre) || "Paciente";
  const ed = p && edad(p.fecha_nacimiento);
  const genero = p ? ({ F: "Femenino", M: "Masculino" }[p.genero] || p.genero) : "";
  const pestanas = [["paciente", "Datos del paciente"]];
  if (r && clinico) pestanas.push(["clinica", "Información clínica"]);
  if (r) pestanas.push(["remision", "Datos de la remisión"]);
  if (r && clinico) pestanas.push(["gestiones", `Gestiones (${contactos.length})`]);
  if (p && clinico) pestanas.push(["imagenes", "Imágenes"]);

  caja.innerHTML = `<section class="tarjeta">
    <div class="ficha-cabeza">
      <span class="avatar">${icono("persona")}</span>
      <div><h3>${esc(nombre)}</h3>
        <div class="meta">${p ? `<span>CC ${esc(p.documento)}</span>` : ""}${ed != null ? `<span>${ed} años</span>` : ""}${genero ? `<span>${esc(genero)}</span>` : ""}${p ? `<span>${esc(p.eps)}</span>` : ""}</div></div>
      ${r ? pildora(r.estado, true) : ""}
    </div>
    <nav class="pestanas" role="tablist">${pestanas.map(([id, t], i) => `<button role="tab" data-tab="${id}" class="${i ? "" : "activa"}">${esc(t)}</button>`).join("")}</nav>
    <div class="ficha-cuerpo"><div id="tab-contenido"></div><div id="ficha-lado"></div></div>
    <div class="nota-info">${icono("info")}<span>Puedes hacer seguimiento al estado de la remisión en tiempo real. Cada cambio queda registrado en la auditoría del sistema.</span></div>
  </section>`;

  $("#ficha-lado", caja).innerHTML = r ? ladoRemision(r, contactos) : `<div class="panel"><p class="tenue">Este paciente no tiene remisiones registradas.</p></div>`;
  const hosp = $("[data-hospital]", caja);
  if (hosp) hosp.addEventListener("click", (e) => { e.preventDefault(); ir("hospitales", { resaltar: r.institucion_destino }); });

  const contexto = { r, p, obs, contactos, caja, recargar: () => pintarFicha(caja, { remisionId, pacienteId }) };
  const mostrar = (tab) => {
    $$(".pestanas button", caja).forEach(b => b.classList.toggle("activa", b.dataset.tab === tab));
    liberarImagenes();
    const cont = $("#tab-contenido", caja);
    ({ paciente: tabPaciente, clinica: tabClinica, remision: tabRemision, gestiones: tabGestiones, imagenes: tabImagenes })[tab](cont, contexto);
  };
  $$(".pestanas button", caja).forEach(b => b.addEventListener("click", () => mostrar(b.dataset.tab)));
  mostrar("paciente");
}

function ladoRemision(r, contactos) {
  // Línea de estado: Solicitud -> En proceso -> Confirmación -> Traslado
  const acepta = [...contactos].reverse().find(c => /acept/i.test(c.respuesta || ""));
  const ultimo = contactos[contactos.length - 1];
  const e = r.estado;
  const pasos = [
    { t: "Solicitud enviada", d: `${fechaCorta(r.fecha_solicitud)}${contactos[0] ? ` · ${fechaHora(contactos[0].fecha).split(",").pop().trim()}` : ""}`, s: "hecho" },
    { t: "En proceso", d: e === "pendiente" ? "En espera de que se inicie la gestión"
        : e === "en_gestion" ? `En espera de confirmación del hospital receptor${contactos.length ? ` · ${contactos.length} contactos` : ""}`
        : `${contactos.length ? `${contactos.length} contacto${contactos.length === 1 ? "" : "s"} con instituciones` : "Gestión realizada"}`,
      s: e === "pendiente" || e === "en_gestion" ? "actual" : "hecho" },
    { t: e === "rechazada" ? "Rechazada" : "Confirmación", d: e === "rechazada" ? (ultimo ? `${esc(ultimo.respuesta)} · ${fechaHora(ultimo.fecha)}` : "La institución no pudo recibir al paciente")
        : e === "aceptada" || e === "completada" ? (acepta ? `Aceptada · ${fechaHora(acepta.fecha)}` : "Aceptada por la institución") : "Pendiente",
      s: e === "rechazada" ? "fallo" : e === "aceptada" || e === "completada" ? "hecho" : "" },
    { t: "Traslado", d: e === "completada" ? "Paciente trasladado" : e === "aceptada" ? "Traslado programado" : e === "rechazada" ? "Cancelado: se debe buscar otra institución" : "Pendiente",
      s: e === "completada" ? "hecho" : e === "aceptada" ? "actual" : "" },
  ];
  const ic = s => s === "hecho" ? icono("check") : s === "fallo" ? icono("x") : s === "actual" ? icono("reloj") : icono("flecha");
  return `<div class="panel">
      <div class="hospital"><span class="hospital-icono"><svg><use href="#hospital"/></svg></span>
        <div><small>Hospital de destino</small><strong>${esc(r.institucion_destino)}</strong>
          <small>${esc(ciudadDe(r.institucion_destino))}</small>
          ${sesion.usuario.rol !== "paciente" ? `<a href="#" data-hospital>Ver detalles del hospital ›</a>` : ""}</div></div>
    </div>
    <div class="panel"><div class="panel-titulo"><h4>Estado de la remisión</h4></div>
      <ol class="linea-estado">${pasos.map(p => `<li class="${p.s}"><span class="paso">${ic(p.s)}</span><div><strong>${esc(p.t)}</strong><small>${p.d}</small></div></li>`).join("")}</ol>
    </div>`;
}

function tabPaciente(cont, { r, p }) {
  const [diag, motivo] = r ? partirMotivo(r.motivo) : ["—", "—"];
  const filas = [
    ["Fecha de nacimiento", p && p.fecha_nacimiento ? fechaCorta(p.fecha_nacimiento) : "—"],
    ["Teléfono", p && p.telefono ? p.telefono.replace(/(\d{3})(\d{3})(\d{4})/, "$1 $2 $3") : "—"],
    ["EPS", p ? p.eps : r && r.eps || "—"],
    ["Tipo de sangre", p && p.tipo_sangre || "—"],
    ["Alergias", p && p.alergias || "—"],
  ];
  if (r && r.motivo) filas.push(["Diagnóstico principal", diag], ["Motivo de remisión", motivo]);
  if (r && sesion.usuario.rol === "paciente") filas.push(["¿Qué significa?", EXPLICA_PACIENTE[r.estado] || "—"]);
  cont.innerHTML = `<div class="panel"><dl class="datos">${filas.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl></div>`;
}

function tabClinica(cont, { r, obs, recargar }) {
  const ultimos = {};
  for (const o of obs) ultimos[o.codigo_loinc || o.tipo] = o;
  const lista = Object.values(ultimos);
  const fuera = lista.filter(o => { const v = VITAL[o.codigo_loinc]; return v && (o.valor < v.min || o.valor > v.max); }).length;
  cont.innerHTML = `<div class="panel"><div class="panel-titulo"><h4>Signos vitales de triage <span class="tenue">· LOINC / UCUM</span></h4>
      <button class="btn btn-suave btn-sm" id="btn-vital">+ Signo vital</button></div>
    ${lista.length ? `<p class="tenue" style="margin:-.4rem 0 .8rem">${fuera ? `${fuera} de ${lista.length} fuera del rango de referencia (en rojo)` : "Todos dentro del rango de referencia"}</p>
      <div class="vitales">${lista.map(o => {
        const v = VITAL[o.codigo_loinc]; const f = v && (o.valor < v.min || o.valor > v.max);
        return `<div class="vital ${f ? "fuera" : ""}" title="${v ? `Referencia: ${v.min}–${v.max}` : ""}"><div class="v">${esc(Number(o.valor).toLocaleString("es-CO"))}<small>${esc(unidadBonita(o.unidad))}</small></div>
          <div class="n">${esc(o.tipo)}</div><div class="c">LOINC ${esc(o.codigo_loinc || "—")}</div></div>`;
      }).join("")}</div>` : `<p class="tenue">Aún no hay signos vitales registrados.</p>`}</div>`;
  $("#btn-vital", cont).addEventListener("click", () => formularioVital(r, recargar));
}

function tabRemision(cont, { r, recargar }) {
  const u = sesion.usuario;
  const clinico = u.rol === "admin" || u.rol === "medico";
  const dueno = u.rol === "admin" || r.creado_por === u.id;
  const filas = [["Número", `#${r.id}`], ["Institución de origen", r.institucion_origen], ["Institución de destino", r.institucion_destino],
    ["Fecha de solicitud", `${fechaCorta(r.fecha_solicitud)} (hace ${diasDesde(r.fecha_solicitud)} días)`], ["Estado", ESTADOS[r.estado] || r.estado]];
  if (r.motivo) filas.push(["Motivo completo", r.motivo]);
  if (r.convenio_vigente !== undefined) filas.push(["Convenio vigente", r.convenio_vigente ? "Sí, hay convenio con la institución de destino" : "No"]);
  if (r.creado_por) filas.push(["Registrada por", `Usuario #${r.creado_por}`]);
  if (r.activo === false) filas.push(["Registro", "Eliminada (soft delete) — solo el admin la ve y puede restaurarla"]);
  cont.innerHTML = `<div class="panel"><dl class="datos">${filas.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${clinico ? `<div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:1.1rem">
      ${r.activo !== false && dueno ? `<button class="btn btn-secundario btn-sm" id="btn-editar">Editar remisión</button><button class="btn btn-peligro btn-sm" id="btn-eliminar">Eliminar</button>` : ""}
      ${r.activo === false && u.rol === "admin" ? `<button class="btn btn-ok btn-sm" id="btn-restaurar">Restaurar</button>` : ""}
      ${r.activo !== false && !dueno ? `<span class="tenue">Solo el médico que la registró (o un administrador) puede editarla o eliminarla.</span>` : ""}</div>` : ""}</div>`;
  const on = (sel, fn) => { const b = $(sel, cont); if (b) b.addEventListener("click", fn); };
  on("#btn-editar", () => formularioRemision(r));
  on("#btn-eliminar", async () => {
    if (!confirm(`¿Eliminar la remisión #${r.id}? Queda en auditoría y un administrador puede restaurarla.`)) return;
    try { await API.del(`/remisiones/${r.id}`); toast(`Remisión #${r.id} eliminada`, "ok"); remisionesCache = null; ir("remisiones"); } catch (e) { toast(e.message, "error"); }
  });
  on("#btn-restaurar", async () => {
    try { await API.post(`/remisiones/${r.id}/restaurar`); toast(`Remisión #${r.id} restaurada`, "ok"); remisionesCache = null; recargar(); } catch (e) { toast(e.message, "error"); }
  });
}

function tabGestiones(cont, { r, contactos, recargar }) {
  cont.innerHTML = `<div class="panel"><div class="panel-titulo"><h4>Bitácora de contactos <span class="tenue">· MongoDB</span></h4>
      <button class="btn btn-suave btn-sm" id="btn-contacto">+ Contacto</button></div>
    ${contactos.length ? `<ul class="bitacora">${contactos.map(c => {
      const resp = String(c.respuesta || "").toLowerCase();
      const clase = /acept/.test(resp) ? "acepta" : /sin cupo|no hay|rechaz|no acept|no disponible|sin respuesta|no tener/.test(resp) ? "niega" : "";
      return `<li class="${clase}"><div class="cuando">${fechaHora(c.fecha)} · ${esc(c.medio)} · ${esc(c.contactado_por)}</div>
        <div class="inst">${esc(c.institucion_contactada)}</div><div class="resp">${esc(c.respuesta)}</div></li>`;
    }).join("")}</ul>` : `<p class="tenue">No hay contactos registrados para esta remisión.</p>`}</div>`;
  $("#btn-contacto", cont).addEventListener("click", () => formularioContacto(r, recargar));
}

// ---------- Imágenes (PACS) ----------
function tabImagenes(cont, { p }) {
  cont.innerHTML = `<div class="panel"><div class="panel-titulo"><h4>Imágenes diagnósticas <span class="tenue">· PACS Orthanc (DICOM)</span></h4></div>
    <form class="subir-imagen" id="form-imagen">
      <div class="campo"><label for="img-archivo">Imagen (PNG o JPEG, máx. 15 MB)</label><input id="img-archivo" type="file" accept="image/png,image/jpeg"></div>
      <div class="campo"><label for="img-desc">Descripción</label><input id="img-desc" maxlength="60" placeholder="Rx de tórax PA"></div>
      <div class="campo"><label for="img-mod">Modalidad</label><select id="img-mod">
        <option value="CR">Radiografía (CR)</option><option value="CT">Tomografía (CT)</option><option value="MR">Resonancia (MR)</option>
        <option value="US">Ecografía (US)</option><option value="OT">Otra (OT)</option></select></div>
      <button class="btn btn-primario" id="btn-subir" type="submit">Subir</button>
    </form>
    <div class="galeria" id="galeria"></div></div>`;
  $("#form-imagen", cont).addEventListener("submit", (e) => subirImagen(e, p));
  cargarGaleria(p);
}

async function cargarGaleria(p) {
  liberarImagenes();
  const v = imagenes.version;
  const gal = $("#galeria");
  if (!gal) return;
  gal.innerHTML = [0, 1, 2].map(() => `<div class="esqueleto" style="aspect-ratio:1"></div>`).join("");
  let lista;
  try { lista = (await API.get(`/pacientes/${p.id}/imagenes`)).imagenes; } catch (e) {
    if (v !== imagenes.version) return;
    gal.innerHTML = `<div class="vacio" style="grid-column:1/-1"><strong>No se pudieron cargar las imágenes</strong><br>${esc(e.message)}<br><br>
      <button class="btn btn-secundario btn-sm" id="reintentar">Reintentar</button></div>`;
    $("#reintentar").addEventListener("click", () => cargarGaleria(p));
    return;
  }
  if (v !== imagenes.version) return;             // el usuario ya cambió de ficha
  if (!lista.length) { gal.innerHTML = `<div class="vacio" style="grid-column:1/-1">Este paciente no tiene imágenes. Sube una con el formulario de arriba.</div>`; return; }
  gal.innerHTML = lista.map(im => `<button class="miniatura" data-id="${esc(im.instance_id)}" type="button">
      <div class="miniatura-img"><div class="esqueleto" style="width:100%;height:100%;border-radius:0"></div></div>
      <div class="miniatura-info"><strong>${esc(im.descripcion)}</strong><small>${esc(im.modalidad_nombre)} · ${fechaDicom(im.fecha)} · ${im.columnas}×${im.filas}px</small></div></button>`).join("");
  lista.forEach(async (im) => {
    const cuadro = $(`[data-id="${im.instance_id}"] .miniatura-img`);
    try {
      // <img src> no puede mandar el token: se descarga con fetch y se muestra desde una URL temporal
      const blob = await API.blob(`/imagenes/${im.instance_id}/preview`);
      const url = URL.createObjectURL(blob);
      if (v !== imagenes.version) return URL.revokeObjectURL(url);
      imagenes.urls.set(im.instance_id, url);
      if (cuadro) cuadro.innerHTML = `<img src="${url}" alt="${esc(im.descripcion)}">`;
    } catch { if (cuadro) cuadro.textContent = "No disponible"; }
  });
  $$(".miniatura", gal).forEach(b => b.addEventListener("click", () => {
    const im = lista.find(x => x.instance_id === b.dataset.id);
    const url = imagenes.urls.get(im.instance_id);
    if (!url) return toast("La imagen aún se está descargando", "info");
    Visor.abrir({ url, titulo: `${im.descripcion} — ${p.nombre}`, subtitulo: `${im.modalidad_nombre} · ${fechaDicom(im.fecha)} · ${im.columnas}×${im.filas}px · CC ${p.documento}` });
  }));
}

async function subirImagen(e, p) {
  e.preventDefault();
  const archivo = $("#img-archivo").files[0];
  if (!archivo) return toast("Elige una imagen primero", "error");
  if (!["image/png", "image/jpeg"].includes(archivo.type)) return toast("Solo se aceptan imágenes PNG o JPEG", "error");
  if (archivo.size > 15 * 1024 * 1024) return toast("La imagen supera los 15 MB", "error");
  const datos = new FormData();
  datos.append("archivo", archivo);
  datos.append("descripcion", $("#img-desc").value.trim() || "Imagen clínica");
  datos.append("modalidad", $("#img-mod").value);
  const btn = $("#btn-subir");
  btn.disabled = true; btn.textContent = "Subiendo…";
  try {
    await API.subir(`/pacientes/${p.id}/imagenes`, datos);
    e.target.reset();
    toast("Imagen guardada en el PACS", "ok");
    cargarGaleria(p);
  } catch (x) { toast(x.message, "error"); } finally { btn.disabled = false; btn.textContent = "Subir"; }
}

// ==========================================================================
// VISTA: REMISIONES
// ==========================================================================
const filtrosRem = { estado: "", texto: "", inactivas: false, paciente: null, mias: false };

async function vistaRemisiones(params = {}) {
  if (params.paciente) filtrosRem.paciente = params.paciente;
  if (params.texto !== undefined) filtrosRem.texto = params.texto;
  const u = sesion.usuario;
  const crea = u.rol === "admin" || u.rol === "medico";
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>${u.rol === "medico" ? "Mis remisiones" : "Remisiones"}</h2>
      <p>${u.rol === "eps" ? `Remisiones de los afiliados de ${esc(u.eps_nombre || "tu EPS")} (solo lectura).` : "Solicitudes de remisión y traslado de pacientes desde y hacia el HUV."}</p></div>
      ${crea ? `<button class="btn btn-primario" id="btn-nueva">+ Nueva remisión</button>` : ""}</div>
    <section class="tarjeta">
      <div class="filtros">
        <div class="buscar">${icono("buscar")}<input type="search" id="f-texto" placeholder="Buscar paciente, documento o institución…" value="${esc(filtrosRem.texto)}"></div>
        <select id="f-estado"><option value="">Todos los estados</option>${Object.entries(ESTADOS).map(([k, v]) => `<option value="${k}" ${filtrosRem.estado === k ? "selected" : ""}>${v}</option>`).join("")}</select>
        ${u.rol === "medico" ? `<label class="check"><input type="checkbox" id="f-mias" ${filtrosRem.mias ? "checked" : ""}> Solo las que registré yo</label>` : ""}
        ${u.rol === "admin" ? `<label class="check"><input type="checkbox" id="f-inactivas" ${filtrosRem.inactivas ? "checked" : ""}> Incluir eliminadas</label>` : ""}
        ${filtrosRem.paciente ? `<span class="pildora est-neutro">Paciente: ${esc(filtrosRem.paciente.nombre)} <button class="btn-icono" style="width:22px;height:22px" id="f-quitar" aria-label="Quitar filtro">${icono("x")}</button></span>` : ""}
        <span class="total" id="total"></span>
      </div>
      <div class="tabla-scroll" id="tabla">${cargando("Cargando remisiones…")}</div>
    </section></div>`;
  if (crea) $("#btn-nueva").addEventListener("click", () => formularioRemision());
  $("#f-texto").addEventListener("input", debounce(e => { filtrosRem.texto = e.target.value; pintar(); }, 200));
  $("#f-estado").addEventListener("change", e => { filtrosRem.estado = e.target.value; pintar(); });
  if ($("#f-mias")) $("#f-mias").addEventListener("change", e => { filtrosRem.mias = e.target.checked; pintar(); });
  if ($("#f-inactivas")) $("#f-inactivas").addEventListener("change", e => { filtrosRem.inactivas = e.target.checked; pintar(); });
  if ($("#f-quitar")) $("#f-quitar").addEventListener("click", () => { filtrosRem.paciente = null; vistaRemisiones(); });

  let todas;
  try { todas = await remisiones(true); } catch (e) { $("#tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
  function pintar() {
    const t = filtrosRem.texto.trim().toLowerCase();
    const filas = todas.filter(r => (filtrosRem.inactivas || r.activo !== false)
      && (!filtrosRem.estado || r.estado === filtrosRem.estado)
      && (!filtrosRem.mias || r.creado_por === u.id)
      && (!filtrosRem.paciente || r.paciente_id === filtrosRem.paciente.id)
      && (!t || [r.paciente_nombre, r.paciente_documento, r.institucion_origen, r.institucion_destino, r.motivo, `#${r.id}`].some(v => String(v || "").toLowerCase().includes(t))))
      .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
    $("#total").textContent = `${filas.length} de ${todas.filter(r => r.activo !== false).length}`;
    if (!filas.length) { $("#tabla").innerHTML = `<div class="vacio">No hay remisiones con esos filtros.</div>`; return; }
    const MAX = 300;
    $("#tabla").innerHTML = `<table><thead><tr><th>Paciente</th><th>Destino</th><th>Solicitud</th><th>Estado</th><th>Convenio</th></tr></thead>
      <tbody>${filas.slice(0, MAX).map(r => `<tr class="clic ${r.activo === false ? "inactiva" : ""}" data-id="${r.id}">
        <td><div class="persona"><span class="avatar">${esc(iniciales(r.paciente_nombre))}</span><div>${esc(r.paciente_nombre || `Paciente ${r.paciente_id}`)}<span class="sub">CC ${esc(r.paciente_documento || "")} · ${esc(r.eps || "")}</span></div></div></td>
        <td>${esc(r.institucion_destino)}<span class="sub">desde ${esc(r.institucion_origen)}</span></td>
        <td class="num">${fechaCorta(r.fecha_solicitud)}<span class="sub">#${r.id}</span></td>
        <td>${pildora(r.estado)}${r.activo === false ? ` <span class="pildora est-neutro">Eliminada</span>` : ""}</td>
        <td>${r.convenio_vigente ? `<span class="convenio-si">Vigente</span>` : `<span class="convenio-no">Sin convenio</span>`}</td></tr>`).join("")}</tbody></table>
      ${filas.length > MAX ? `<div class="vacio">Se muestran las ${MAX} más recientes. Usa la búsqueda o los filtros para ver las demás.</div>` : ""}`;
    $$("#tabla tr.clic").forEach(tr => tr.addEventListener("click", () => ir("ficha", { remision: +tr.dataset.id, desde: "remisiones" })));
  }
  pintar();
}

// ==========================================================================
// VISTA: PACIENTES
// ==========================================================================
async function vistaPacientes() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Pacientes</h2><p>Busca por número de documento o por nombre.</p></div>
      <button class="btn btn-primario" id="btn-nuevo">+ Nuevo paciente</button></div>
    <section class="tarjeta"><div class="filtros"><div class="buscar">${icono("buscar")}<input type="search" id="p-buscar" placeholder="Documento o nombre…" autocomplete="off"></div>
      <span class="total" id="p-total"></span></div><div class="tabla-scroll" id="p-tabla">${cargando("Cargando pacientes…")}</div></section></div>`;
  $("#btn-nuevo").addEventListener("click", formularioPaciente);
  const buscar = async (q) => {
    try {
      const filas = await API.get(`/pacientes${q ? `?q=${encodeURIComponent(q)}` : ""}`);
      $("#p-total").textContent = `${filas.length} paciente${filas.length === 1 ? "" : "s"}`;
      $("#p-tabla").innerHTML = filas.length ? `<table><thead><tr><th>Paciente</th><th>Edad</th><th>EPS</th><th>Tipo de sangre</th><th>Teléfono</th></tr></thead>
        <tbody>${filas.slice(0, 300).map(p => `<tr class="clic" data-pac="${p.id}">
          <td><div class="persona"><span class="avatar">${esc(iniciales(p.nombre))}</span><div>${esc(p.nombre)}<span class="sub">CC ${esc(p.documento)} · ${esc({ F: "Femenino", M: "Masculino" }[p.genero] || p.genero)}</span></div></div></td>
          <td class="num">${edad(p.fecha_nacimiento) ?? "—"}</td><td>${esc(p.eps)}</td><td>${esc(p.tipo_sangre || "—")}</td><td class="num">${esc(p.telefono || "—")}</td></tr>`).join("")}</tbody></table>
        ${filas.length > 300 ? `<div class="vacio">Se muestran los primeros 300. Afina la búsqueda.</div>` : ""}`
        : `<div class="vacio">No se encontró ningún paciente con “${esc(q)}”.</div>`;
      $$("[data-pac]").forEach(tr => tr.addEventListener("click", async () => {
        const id = +tr.dataset.pac;
        const rems = (await remisiones()).filter(r => r.paciente_id === id && r.activo !== false)
          .sort((a, b) => String(b.fecha_solicitud).localeCompare(String(a.fecha_solicitud)));
        ir("ficha", { remision: rems[0] && rems[0].id, paciente: id, desde: "pacientes" });
      }));
    } catch (e) { $("#p-tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; }
  };
  $("#p-buscar").addEventListener("input", debounce(e => buscar(e.target.value.trim()), 300));
  buscar("");
}

// ==========================================================================
// VISTA: HOSPITALES DE DESTINO
// ==========================================================================
// Texto sobre fondo blanco: tonos oscuros legibles (los de la barra son para rellenos)
const TEXTO_ESTADO = { completada: "var(--violeta)", aceptada: "var(--ok)", en_gestion: "#a86a05", pendiente: "#a86a05", rechazada: "var(--error)" };
const COLOR_ESTADO = { completada: "var(--violeta)", aceptada: "var(--ok)", en_gestion: "#e0a030", pendiente: "#f2c46b", rechazada: "var(--error)" };

async function vistaHospitales({ resaltar } = {}) {
  $("#contenido").innerHTML = `<div class="pagina"><div class="pagina-cabeza"><div><h2>Hospitales de destino</h2>
    <p>Cómo responde cada institución a las remisiones: volumen, aceptación, rechazos y convenios.</p></div></div>
    <div class="leyenda">${Object.entries(COLOR_ESTADO).map(([k, c]) => `<span><i style="background:${c}"></i>${ESTADOS[k]}</span>`).join("")}</div>
    <div id="hosp">${cargando()}</div></div>`;
  let lista;
  try { lista = activas(await remisiones()); } catch (e) { $("#hosp").innerHTML = `<div class="tarjeta vacio">${esc(e.message)}</div>`; return; }
  const grupos = {};
  for (const r of lista) (grupos[r.institucion_destino] ||= []).push(r);
  const orden = Object.entries(grupos).sort((a, b) => b[1].length - a[1].length);
  $("#hosp").innerHTML = `<div class="hospitales">${orden.map(([inst, rs]) => {
    const c = k => rs.filter(r => r.estado === k).length;
    const exito = c("aceptada") + c("completada");
    return `<article class="tarjeta hospital-tarjeta ${inst === resaltar ? "resaltado" : ""}" ${inst === resaltar ? 'id="resaltado"' : ""}>
      <div class="hospital"><span class="hospital-icono"><svg><use href="#hospital"/></svg></span>
        <div><strong>${esc(inst)}</strong><small>${esc(ciudadDe(inst))}</small></div></div>
      <div class="barra-dist" role="img" aria-label="Distribución por estado">${Object.keys(COLOR_ESTADO).map(k => c(k)
        ? `<span class="marca-dato" style="width:${(100 * c(k)) / rs.length}%;background:${COLOR_ESTADO[k]}" data-tip="${ESTADOS[k]}: ${c(k)} de ${rs.length} (${pct(c(k), rs.length)}%)"></span>` : "").join("")}</div>
      <div class="mini-datos"><div><strong>${rs.length}</strong><small>Remisiones</small></div>
        <div><strong>${pct(exito, rs.length)}%</strong><small>Aceptadas</small></div>
        <div><strong>${pct(c("rechazada"), rs.length)}%</strong><small>Rechazadas</small></div></div>
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem">
        <span class="tenue">${pct(rs.filter(r => r.convenio_vigente).length, rs.length)}% con convenio vigente</span>
        <button class="btn btn-secundario btn-sm" data-inst="${esc(inst)}">Ver remisiones</button></div></article>`;
  }).join("")}</div>`;
  $$("[data-inst]").forEach(b => b.addEventListener("click", () => { filtrosRem.estado = ""; filtrosRem.paciente = null; ir("remisiones", { texto: b.dataset.inst }); }));
  activarTooltips($("#hosp"));
  const r = $("#resaltado");
  if (r) r.scrollIntoView({ behavior: "smooth", block: "center" });
}

// ==========================================================================
// VISTA: REPORTES (gráficas SVG, un solo tono, etiquetas directas + tabla)
// ==========================================================================
async function vistaReportes() {
  $("#contenido").innerHTML = `<div class="pagina"><div class="pagina-cabeza"><div><h2>Reportes</h2>
    <p>Indicadores de las remisiones visibles para tu rol. Pasa el cursor sobre las barras para ver el detalle.</p></div></div>
    <div id="rep">${cargando()}</div></div>`;
  let lista;
  try { lista = activas(await remisiones()); } catch (e) { $("#rep").innerHTML = `<div class="tarjeta vacio">${esc(e.message)}</div>`; return; }
  const N = lista.length;
  const exito = r => r.estado === "aceptada" || r.estado === "completada";

  const porEstado = Object.entries(ESTADOS).map(([k, v]) => ({ etiqueta: v, valor: lista.filter(r => r.estado === k).length }));
  const meses = {};
  for (const r of lista) { const m = String(r.fecha_solicitud).slice(0, 7); meses[m] = (meses[m] || 0) + 1; }
  const porMes = Object.keys(meses).sort().map(m => ({ etiqueta: `${MESES[+m.slice(5, 7) - 1]} ${m.slice(2, 4)}`, valor: meses[m] }));
  const dest = {};
  for (const r of lista) (dest[r.institucion_destino] ||= []).push(r);
  const rechazo = Object.entries(dest).filter(([, rs]) => rs.length >= 5)
    .map(([k, rs]) => ({ etiqueta: k.replace("Hospital ", "H. ").replace("Clínica ", "C. ").replace("Fundación ", "F. "), valor: pct(rs.filter(r => r.estado === "rechazada").length, rs.length), n: rs.length, sufijo: "%" }))
    .sort((a, b) => b.valor - a.valor).slice(0, 8);
  const con = lista.filter(r => r.convenio_vigente), sin = lista.filter(r => !r.convenio_vigente);
  const convenio = [{ etiqueta: "Con convenio", valor: pct(con.filter(exito).length, con.length), n: con.length, sufijo: "%" },
                    { etiqueta: "Sin convenio", valor: pct(sin.filter(exito).length, sin.length), n: sin.length, sufijo: "%" }];

  $("#rep").innerHTML = `<div class="kpis" style="margin-bottom:1.1rem">
      ${kpi("remisiones", "tono-azul", N, "Remisiones activas")}
      ${kpi("check", "tono-verde", `${pct(lista.filter(exito).length, N)}%`, "Aceptadas o completadas")}
      ${kpi("x", "tono-rojo", `${pct(lista.filter(r => r.estado === "rechazada").length, N)}%`, "Rechazadas")}
      ${kpi("hospitales", "tono-violeta", Object.keys(dest).length, "Instituciones de destino")}</div>
    <div class="graficas">
      ${tarjetaGrafica("Remisiones por estado", `Total: ${N}`, barrasH(porEstado, N))}
      ${tarjetaGrafica("Remisiones por mes de solicitud", "Número de solicitudes nuevas", barrasV(porMes))}
      ${tarjetaGrafica("Tasa de rechazo por institución de destino", "Instituciones con 5 o más remisiones · % rechazadas", barrasH(rechazo, 100))}
      ${tarjetaGrafica("Éxito de la remisión según convenio", "% de remisiones aceptadas o completadas", barrasH(convenio, 100))}
    </div>`;
  activarTooltips($("#rep"));
}

function tarjetaGrafica(titulo, sub, { svg, tabla }) {
  return `<section class="tarjeta tarjeta-cuerpo grafica"><h4>${esc(titulo)}</h4><p class="tenue">${esc(sub)}</p>${svg}
    <details style="margin-top:.6rem"><summary class="tenue" style="cursor:pointer">Ver como tabla</summary>${tabla}</details></section>`;
}
function tablaDatos(datos) {
  return `<table style="margin-top:.5rem"><tbody>${datos.map(d => `<tr><td>${esc(d.etiqueta)}</td><td class="num" style="text-align:right">${d.valor}${d.sufijo || ""}${d.n != null ? ` <span class="tenue">(n=${d.n})</span>` : ""}</td></tr>`).join("")}</tbody></table>`;
}
// Barras horizontales: etiqueta a la izquierda, valor al final de la barra
function barrasH(datos, maximo) {
  const W = 560, izq = 190, alto = 30, H = datos.length * alto + 8, ancho = W - izq - 60;
  const max = Math.max(maximo || 0, ...datos.map(d => d.valor), 1);
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img">${datos.map((d, i) => {
    const y = 4 + i * alto, w = Math.max(2, (d.valor / max) * ancho);
    return `<text class="etq" x="${izq - 10}" y="${y + 18}" text-anchor="end">${esc(d.etiqueta.length > 28 ? d.etiqueta.slice(0, 27) + "…" : d.etiqueta)}</text>
      <rect class="marca-dato" x="${izq}" y="${y + 5}" width="${w}" height="18" rx="4" fill="var(--azul)" data-tip="${esc(d.etiqueta)}: ${d.valor}${d.sufijo || ""}${d.n != null ? ` (n=${d.n})` : ""}"/>
      <text class="val" x="${izq + w + 8}" y="${y + 18}">${d.valor}${d.sufijo || ""}</text>`;
  }).join("")}<line class="eje" x1="${izq}" x2="${izq}" y1="0" y2="${H}"/></svg>`;
  return { svg, tabla: tablaDatos(datos) };
}
// Barras verticales (serie de tiempo)
function barrasV(datos) {
  const W = 560, H = 230, base = 196, top = 16, izq = 10;
  const max = Math.max(...datos.map(d => d.valor), 1);
  const paso = (W - izq * 2) / Math.max(datos.length, 1), bw = Math.min(46, paso - 8);
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img">
    <line class="eje" x1="0" x2="${W}" y1="${base}" y2="${base}"/>${datos.map((d, i) => {
      const h = ((base - top) * d.valor) / max, x = izq + i * paso + (paso - bw) / 2;
      return `<rect class="marca-dato" x="${x}" y="${base - h}" width="${bw}" height="${h}" rx="4" fill="var(--azul)" data-tip="${esc(d.etiqueta)}: ${d.valor} remisiones"/>
        <text class="val" x="${x + bw / 2}" y="${base - h - 6}" text-anchor="middle">${d.valor}</text>
        <text class="etq" x="${x + bw / 2}" y="${base + 18}" text-anchor="middle">${esc(d.etiqueta)}</text>`;
    }).join("")}</svg>`;
  return { svg, tabla: tablaDatos(datos) };
}
function activarTooltips(raiz) {
  const tip = $("#tooltip");
  $$("[data-tip]", raiz).forEach(el => {
    el.addEventListener("mousemove", (e) => { tip.textContent = el.dataset.tip; tip.classList.remove("oculto"); tip.style.left = `${e.clientX + 14}px`; tip.style.top = `${e.clientY + 14}px`; });
    el.addEventListener("mouseleave", () => tip.classList.add("oculto"));
  });
}

// ==========================================================================
// VISTA: USUARIOS (admin) — aquí se desbloquea
// ==========================================================================
async function vistaUsuarios() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Usuarios</h2><p>Un usuario se bloquea al tercer intento fallido de contraseña. Solo el administrador lo desbloquea.</p></div>
      <button class="btn btn-primario" id="btn-nuevo-usr">+ Nuevo usuario</button></div>
    <div id="u-kpis"></div>
    <section class="tarjeta"><div class="filtros"><label class="check"><input type="checkbox" id="u-bloq"> Ver solo bloqueados</label><span class="total" id="u-total"></span></div>
      <div class="tabla-scroll" id="u-tabla">${cargando()}</div></section></div>`;
  $("#btn-nuevo-usr").addEventListener("click", formularioUsuario);
  $("#u-bloq").addEventListener("change", pintar);
  let usuarios = [];
  async function cargar() {
    try {
      usuarios = await API.get("/usuarios");
      $("#u-kpis").innerHTML = `<div class="kpis">${kpi("usuarios", "tono-azul", usuarios.length, "Usuarios")}
        ${kpi("candado", "tono-rojo", usuarios.filter(u => u.bloqueado).length, "Bloqueados")}
        ${kpi("alerta", "tono-ambar", usuarios.filter(u => !u.bloqueado && u.intentos_fallidos > 0).length, "Con intentos fallidos")}</div>`;
      pintar();
    } catch (e) { $("#u-tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; }
  }
  function pintar() {
    const solo = $("#u-bloq").checked;
    const filas = usuarios.filter(u => !solo || u.bloqueado);
    $("#u-total").textContent = `${filas.length} de ${usuarios.length}`;
    $("#u-tabla").innerHTML = filas.length ? `<table><thead><tr><th>Usuario</th><th>Rol</th><th>Estado</th><th>Intentos fallidos</th><th></th></tr></thead>
      <tbody>${filas.map(u => `<tr><td><div class="persona"><span class="avatar">${esc(iniciales(u.nombre))}</span><div>${esc(u.nombre)}<span class="sub">${esc(u.correo)}${u.eps_nombre ? ` · ${esc(u.eps_nombre)}` : ""}</span></div></div></td>
        <td><span class="rol rol-${esc(u.rol)}">${esc(ROLES[u.rol] || u.rol)}</span></td>
        <td>${u.bloqueado ? `<span class="pildora est-bloqueado">${icono("candado")}Bloqueado</span>` : u.activo ? `<span class="pildora est-activo">Activo</span>` : `<span class="pildora est-neutro">Inactivo</span>`}</td>
        <td class="num">${u.intentos_fallidos} / 3</td>
        <td>${u.bloqueado ? `<button class="btn btn-ok btn-sm" data-desbloquear="${u.id}">Desbloquear</button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : `<div class="vacio">${solo ? "No hay usuarios bloqueados." : "No hay usuarios."}</div>`;
    $$("[data-desbloquear]").forEach(b => b.addEventListener("click", async () => {
      const u = usuarios.find(x => x.id === +b.dataset.desbloquear);
      if (!confirm(`¿Desbloquear a ${u.correo}? Su contador de intentos vuelve a 0.`)) return;
      try { await API.post(`/usuarios/${u.id}/desbloquear`); toast(`${u.correo} desbloqueado (queda en auditoría)`, "ok"); cargar(); } catch (e) { toast(e.message, "error"); }
    }));
  }
  cargar();
}

// ==========================================================================
// VISTA: AUDITORÍA (admin)
// ==========================================================================
const ACCIONES = {
  login_fallido: "Login fallido", bloqueo_usuario: "Bloqueo de usuario", login_bloqueado: "Intento con usuario bloqueado",
  desbloqueo_usuario: "Desbloqueo", login_exitoso: "Login exitoso", imagen_subida: "Imagen subida", imagen_vista: "Imagen vista",
  soft_edit: "Edición (soft edit)", soft_delete: "Eliminación (soft delete)", restaurar: "Restauración",
  usuario_creado: "Usuario creado", paciente_creado: "Paciente creado", remision_creada: "Remisión creada",
};
async function vistaAuditoria() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Auditoría</h2><p>Quién hizo qué y cuándo (hora de Colombia): intentos fallidos, bloqueos, desbloqueos, imágenes y cambios.</p></div>
      <button class="btn btn-secundario" id="a-ref">Actualizar</button></div>
    <div id="a-kpis"></div>
    <section class="tarjeta"><div class="filtros"><select id="a-accion"><option value="">Todas las acciones</option>
      ${Object.entries(ACCIONES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select><span class="total" id="a-total"></span></div>
      <div class="tabla-scroll" id="a-tabla">${cargando()}</div></section></div>`;
  async function cargar() {
    const accion = $("#a-accion").value;
    $("#a-tabla").innerHTML = cargando();
    try {
      const [filas, todas] = await Promise.all([API.get(`/auditoria?limite=300${accion ? `&accion=${accion}` : ""}`), API.get("/auditoria?limite=1000")]);
      const c = a => todas.filter(f => f.accion === a).length;
      $("#a-kpis").innerHTML = `<div class="kpis">${kpi("alerta", "tono-ambar", c("login_fallido"), "Logins fallidos")}
        ${kpi("candado", "tono-rojo", c("bloqueo_usuario"), "Bloqueos")}${kpi("x", "tono-rojo", c("login_bloqueado"), "Intentos con usuario bloqueado")}
        ${kpi("usuarios", "tono-violeta", c("desbloqueo_usuario"), "Desbloqueos")}${kpi("imagen", "tono-azul", c("imagen_subida") + c("imagen_vista"), "Accesos a imágenes")}</div>`;
      $("#a-total").textContent = `${filas.length} evento${filas.length === 1 ? "" : "s"}`;
      $("#a-tabla").innerHTML = filas.length ? `<table><thead><tr><th>Fecha y hora</th><th>Usuario</th><th>Acción</th><th>Recurso</th><th>Detalle</th></tr></thead>
        <tbody>${filas.map(f => `<tr><td class="num" style="white-space:nowrap">${fechaHora(f.fecha)}</td><td>${esc(f.correo || (f.usuario_id ? `#${f.usuario_id}` : "—"))}</td>
          <td><span class="accion ${ACCIONES[f.accion] ? `ac-${esc(f.accion)}` : "ac-otra"}">${esc(f.accion)}</span></td>
          <td class="num" style="white-space:nowrap">${esc(f.tabla)}${f.registro_id ? ` #${f.registro_id}` : ""}</td><td>${esc(f.detalle || "")}</td></tr>`).join("")}</tbody></table>`
        : `<div class="vacio">No hay eventos para ese filtro.</div>`;
    } catch (e) { $("#a-tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; }
  }
  $("#a-accion").addEventListener("change", cargar);
  $("#a-ref").addEventListener("click", cargar);
  cargar();
}

// ==========================================================================
// FORMULARIOS
// ==========================================================================
const datalist = (id, items) => `<datalist id="${id}">${items.map(i => `<option value="${esc(i)}">`).join("")}</datalist>`;

async function formularioRemision(r = null) {
  let lista;
  try { lista = await pacientes(); } catch (e) { toast(e.message, "error"); return; }
  const etq = p => `${p.documento} — ${p.nombre}`;
  const actual = r ? lista.find(p => p.id === r.paciente_id) : null;
  abrirFormulario({
    titulo: r ? `Editar remisión #${r.id}` : "Nueva remisión", textoBoton: r ? "Guardar cambios" : "Crear remisión",
    campos: `<div class="campo"><label for="m-pac">Paciente (documento o nombre)</label>
        <input id="m-pac" name="paciente" list="dl-pac" required value="${actual ? esc(etq(actual)) : ""}" placeholder="Escribe para buscar…">${datalist("dl-pac", lista.map(etq))}</div>
      <div class="fila"><div class="campo"><label for="m-ori">Institución de origen</label><input id="m-ori" name="institucion_origen" list="dl-inst" required value="${esc(r ? r.institucion_origen : "Hospital Universitario del Valle")}"></div>
        <div class="campo"><label for="m-des">Institución de destino</label><input id="m-des" name="institucion_destino" list="dl-inst" required value="${esc(r ? r.institucion_destino : "")}"></div></div>
      ${datalist("dl-inst", INSTITUCIONES)}
      <div class="fila"><div class="campo"><label for="m-fec">Fecha de solicitud</label><input id="m-fec" type="date" name="fecha_solicitud" required value="${esc(r ? String(r.fecha_solicitud).slice(0, 10) : hoyISO())}"></div>
        <div class="campo"><label for="m-est">Estado</label><select id="m-est" name="estado">${Object.entries(ESTADOS).map(([k, v]) => `<option value="${k}" ${(r ? r.estado : "pendiente") === k ? "selected" : ""}>${v}</option>`).join("")}</select></div></div>
      <div class="campo"><label for="m-mot">Motivo (diagnóstico; lo que requiere)</label><textarea id="m-mot" name="motivo" required placeholder="Neumonía adquirida en la comunidad; requiere valoración por neumología">${esc(r ? r.motivo : "")}</textarea></div>
      <label class="check"><input type="checkbox" name="convenio_vigente" ${r && r.convenio_vigente ? "checked" : ""}> Hay convenio vigente con la institución de destino</label>`,
    async alGuardar(d) {
      const p = lista.find(x => etq(x) === d.paciente) || lista.find(x => x.documento === d.paciente.trim());
      if (!p) throw new Error("Escoge un paciente de la lista (o créalo primero en Pacientes).");
      for (const [c, n] of [["institucion_origen", "la institución de origen"], ["institucion_destino", "la institución de destino"], ["motivo", "el motivo"]])
        if (!String(d[c] || "").trim()) throw new Error(`Falta ${n}.`);
      const cuerpo = { paciente_id: p.id, institucion_origen: d.institucion_origen.trim(), institucion_destino: d.institucion_destino.trim(),
        fecha_solicitud: d.fecha_solicitud, motivo: d.motivo.trim(), estado: d.estado, convenio_vigente: d.convenio_vigente };
      const g = r ? await API.put(`/remisiones/${r.id}`, cuerpo) : await API.post("/remisiones", cuerpo);
      toast(r ? `Remisión #${g.id} actualizada (queda el historial)` : `Remisión #${g.id} creada`, "ok");
      remisionesCache = null;
      actualizarNotificaciones();
      ir("ficha", { remision: g.id, desde: "remisiones" });
    },
  });
}

function formularioVital(r, recargar) {
  abrirFormulario({
    titulo: `Signo vital · remisión #${r.id}`, textoBoton: "Registrar",
    campos: `<div class="campo"><label for="m-vit">Signo vital (código LOINC)</label><select id="m-vit" name="loinc">${VITALES.map(v => `<option value="${v.loinc}">${esc(v.tipo)} · ${v.loinc}</option>`).join("")}</select></div>
      <div class="fila"><div class="campo"><label for="m-val">Valor</label><input id="m-val" name="valor" type="number" step="any" required></div>
        <div class="campo"><label>Unidad (UCUM)</label><input id="m-uni" disabled></div></div><p class="tenue" id="m-rango" style="margin:-.3rem 0 .9rem"></p>`,
    alAbrir(form) {
      const s = $("#m-vit", form);
      const f = () => { const v = VITAL[s.value]; $("#m-uni", form).value = v.unidad; $("#m-rango", form).textContent = `Rango de referencia en adultos: ${v.min} – ${v.max} ${unidadBonita(v.unidad)}`; };
      s.addEventListener("change", f); f();
    },
    async alGuardar(d) {
      const v = VITAL[d.loinc];
      if (d.valor === "" || isNaN(+d.valor)) throw new Error("Escribe un valor numérico.");
      await API.post("/observaciones", { remision_id: r.id, tipo: v.tipo, codigo_loinc: v.loinc, valor: +d.valor, unidad: v.unidad });
      toast(`${v.tipo} registrada`, "ok");
      recargar();
    },
  });
}

function formularioContacto(r, recargar) {
  abrirFormulario({
    titulo: `Nuevo contacto · remisión #${r.id}`, textoBoton: "Registrar contacto",
    campos: `<div class="campo"><label for="m-ins">Institución contactada</label><input id="m-ins" name="institucion_contactada" list="dl-inst2" required>${datalist("dl-inst2", INSTITUCIONES)}</div>
      <div class="fila"><div class="campo"><label for="m-med">Medio</label><select id="m-med" name="medio"><option value="telefono">Teléfono</option><option value="plataforma">Plataforma de referencia</option><option value="correo">Correo</option></select></div>
        <div class="campo"><label for="m-por">Contactado por</label><input id="m-por" name="contactado_por" required value="${esc(sesion.usuario.nombre)}"></div></div>
      <div class="campo"><label for="m-res">Respuesta de la institución</label><input id="m-res" name="respuesta" required placeholder="Sin cupo en UCI / Acepta paciente"></div>`,
    async alGuardar(d) {
      if (!d.institucion_contactada.trim() || !d.respuesta.trim()) throw new Error("Completa la institución y la respuesta.");
      const nuevo = { fecha: new Date().toISOString(), medio: d.medio, institucion_contactada: d.institucion_contactada.trim(), contactado_por: d.contactado_por.trim(), respuesta: d.respuesta.trim() };
      const docs = await API.get(`/gestiones-contacto?remision_id=${r.id}`);     // un documento de Mongo por remisión
      if (docs.length) await API.put(`/gestiones-contacto/${docs[0]._id}`, { remision_id: r.id, contactos: [...(docs[0].contactos || []), nuevo] });
      else await API.post("/gestiones-contacto", { remision_id: r.id, contactos: [nuevo] });
      toast("Contacto registrado en la bitácora", "ok");
      recargar();
    },
  });
}

function formularioPaciente() {
  abrirFormulario({
    titulo: "Nuevo paciente", textoBoton: "Crear paciente",
    campos: `<div class="campo"><label for="m-nom">Nombre completo</label><input id="m-nom" name="nombre" required></div>
      <div class="fila-3"><div class="campo"><label for="m-doc">Documento</label><input id="m-doc" name="documento" required inputmode="numeric"></div>
        <div class="campo"><label for="m-gen">Género</label><select id="m-gen" name="genero"><option value="F">Femenino</option><option value="M">Masculino</option></select></div>
        <div class="campo"><label for="m-eps">EPS</label><input id="m-eps" name="eps" list="dl-eps" required>${datalist("dl-eps", EPS)}</div></div>
      <div class="fila"><div class="campo"><label for="m-nac">Fecha de nacimiento</label><input id="m-nac" name="fecha_nacimiento" type="date"></div>
        <div class="campo"><label for="m-tel">Teléfono</label><input id="m-tel" name="telefono" inputmode="tel" maxlength="15"></div></div>
      <div class="fila"><div class="campo"><label for="m-san">Tipo de sangre</label><select id="m-san" name="tipo_sangre"><option value="">—</option>${["O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-"].map(t => `<option>${t}</option>`).join("")}</select></div>
        <div class="campo"><label for="m-ale">Alergias</label><input id="m-ale" name="alergias" placeholder="Sin alergias conocidas"></div></div>`,
    async alGuardar(d) {
      if (!d.nombre.trim() || !d.documento.trim() || !d.eps.trim()) throw new Error("Completa nombre, documento y EPS.");
      const cuerpo = { nombre: d.nombre.trim(), documento: d.documento.trim(), genero: d.genero, eps: d.eps.trim() };
      for (const k of ["fecha_nacimiento", "telefono", "tipo_sangre", "alergias"]) if (String(d[k] || "").trim()) cuerpo[k] = d[k].trim();
      const p = await API.post("/pacientes", cuerpo);
      pacientesCache = null;
      toast(`Paciente ${p.nombre} creado`, "ok");
      vistaPacientes();
    },
  });
}

function formularioUsuario() {
  abrirFormulario({
    titulo: "Nuevo usuario", textoBoton: "Crear usuario",
    campos: `<div class="campo"><label for="m-un">Nombre</label><input id="m-un" name="nombre" required></div>
      <div class="fila"><div class="campo"><label for="m-uc">Usuario (correo)</label><input id="m-uc" name="correo" required maxlength="50" placeholder="nombre@huv.gov.co"></div>
        <div class="campo"><label for="m-ucl">Contraseña</label><input id="m-ucl" name="contrasena" type="password" required minlength="6"></div></div>
      <p class="tenue" style="margin:-.4rem 0 .9rem">El usuario solo admite letras, números y . _ - @ (máximo 50). La contraseña, mínimo 6 caracteres.</p>
      <div class="fila"><div class="campo"><label for="m-ur">Rol</label><select id="m-ur" name="rol">${Object.entries(ROLES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select></div>
        <div class="campo oculto" id="c-eps"><label for="m-ue">EPS</label><input id="m-ue" name="eps_nombre" list="dl-eps2">${datalist("dl-eps2", EPS)}</div>
        <div class="campo oculto" id="c-pac"><label for="m-up">Documento del paciente</label><input id="m-up" name="documento_paciente" inputmode="numeric"></div></div>`,
    alAbrir(form) {
      const rol = $("#m-ur", form);
      const f = () => { $("#c-eps", form).classList.toggle("oculto", rol.value !== "eps"); $("#c-pac", form).classList.toggle("oculto", rol.value !== "paciente"); };
      rol.addEventListener("change", f); f();
    },
    async alGuardar(d) {
      const cuerpo = { nombre: d.nombre.trim(), correo: d.correo.trim(), contrasena: d.contrasena, rol: d.rol };
      if (d.rol === "eps") { if (!d.eps_nombre.trim()) throw new Error("Escribe la EPS del usuario."); cuerpo.eps_nombre = d.eps_nombre.trim(); }
      if (d.rol === "paciente") {
        const l = await API.get(`/pacientes?q=${encodeURIComponent(d.documento_paciente.trim())}`);
        const p = l.find(x => x.documento === d.documento_paciente.trim());
        if (!p) throw new Error("No existe un paciente con ese documento.");
        cuerpo.paciente_id = p.id;
      }
      const n = await API.post("/usuarios", cuerpo);
      toast(`Usuario ${n.correo} creado`, "ok");
      vistaUsuarios();
    },
  });
}

// ==========================================================================
// ARRANQUE
// ==========================================================================
(async function iniciar() {
  pintarServicios();
  setInterval(() => { if (!sesion.usuario) pintarServicios(); }, 15000);
  setInterval(() => { if (sesion.usuario) actualizarNotificaciones(); }, 60000);
  const d = API.datosToken();
  if (d && d.exp * 1000 > Date.now()) await entrar(); else API.salir();
})();
