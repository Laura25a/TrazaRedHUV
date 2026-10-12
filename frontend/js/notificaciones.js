// ==========================================================================
// TrazaRed HUV — bandeja de notificaciones y trabajo pendiente (R18, Persona 4)
// ==========================================================================
// La campanita ahora muestra la bandeja REAL del usuario (GET /notificaciones:
// una fila por aviso, con su "leida") y los contadores de trabajo
// (GET /pendientes). Conserva la vista de "remisiones que esperan respuesta"
// de la Persona 1: primero corre su render y encima se agregan los bloques
// nuevos. Solo el dueño puede marcar una notificación como leída (el API
// responde 403 ante una ajena).
//
// NO se modificó app.js: se captura su actualizarNotificaciones() y se la
// envuelve. Los avisos en vivo llegan por eventos.js (R17).
const Notificaciones = (() => {
  const baseActualizar = window.actualizarNotificaciones;   // la de la Persona 1

  async function refrescar() {
    if (typeof sesion === "undefined" || !sesion.usuario) return;
    try { await baseActualizar(); } catch { /* la campana es informativa */ }
    try {
      const [bandeja, pend] = await Promise.all([
        API.get("/notificaciones"),
        API.get("/pendientes"),
      ]);
      pintarBandeja(bandeja);
      pintarPendientes(pend);
    } catch { /* si falla, no bloquea la pantalla */ }
    // Auto-reparación: si el canal en vivo se cayó, se vuelve a abrir.
    if (window.Eventos && !Eventos.conectado()) Eventos.iniciar();
  }

  function pintarBandeja(lista) {
    const panel = $("#panel-notif");
    if (!panel) return;
    const sinLeer = lista.filter(n => !n.leida).length;
    const insignia = $("#insignia");
    if (insignia) {
      insignia.textContent = sinLeer > 99 ? "99+" : sinLeer;
      insignia.classList.toggle("oculto", !sinLeer);
    }
    const bloque = document.createElement("div");
    bloque.className = "bandeja-bloque";
    bloque.innerHTML = `<h4>Notificaciones ${sinLeer ? `· ${sinLeer} sin leer` : ""}</h4>` + (
      lista.length
        ? lista.slice(0, 6).map(n => `
          <button class="fila-notif ${n.leida ? "leida" : ""}" data-notif="${n.id}" title="${n.leida ? "Ya leída" : "Marcar como leída"}">
            <span class="punto" aria-hidden="true"></span>
            <span class="fila-notif-texto">${esc(n.mensaje)}
              <small>${esc(fechaHora(n.fecha))}${n.leida ? " · leída" : ""}</small>
            </span>
          </button>`).join("")
        : `<p class="tenue" style="padding:.4rem .7rem">Sin notificaciones.</p>`
    );
    // Los bloques nuevos van encima del render de la Persona 1.
    panel.querySelectorAll(".bandeja-bloque, .pendientes-strip").forEach(b => b.remove());
    panel.prepend(bloque);
    panel.querySelectorAll("[data-notif]").forEach(b =>
      b.addEventListener("click", () => marcarLeida(+b.dataset.notif)));
  }

  function pintarPendientes(p) {
    if (!["admin", "medico", "especialista"].includes(sesion.usuario.rol)) return;
    const panel = $("#panel-notif");
    if (!panel) return;
    const tira = document.createElement("div");
    tira.className = "pendientes-strip";
    tira.innerHTML = `
      <span class="pendiente">${p.reportes_por_revisar} reportes por revisar</span>
      <span class="pendiente">${p.remisiones_recibidas} remisiones recibidas</span>
      <span class="pendiente">${p.alertas_activas} alertas activas</span>`;
    panel.prepend(tira);
  }

  async function marcarLeida(id) {
    try {
      await API.post(`/notificaciones/${id}/leer`);
      await refrescar();
    } catch { /* si no es propia, el API ya respondió 403 */ }
  }

  return { refrescar };
})();

// Se expone en window para que eventos.js lo encuentre (const no se publica solo).
window.Notificaciones = Notificaciones;

// Todas las llamadas existentes de app.js (entrar, cambios y el timer de 60 s)
// pasan por la versión con bandeja.
window.actualizarNotificaciones = () => Notificaciones.refrescar();
