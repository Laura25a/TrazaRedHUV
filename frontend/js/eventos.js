// ==========================================================================
// TrazaRed HUV — canal en tiempo real (R17, Persona 4)
// ==========================================================================
// Se conecta a GET /api/eventos/stream (Server-Sent Events) al iniciar sesión
// y reparte cada evento sin recargar: refresca la bandeja (R18), los
// contadores de trabajo pendiente (R18) y las alertas críticas (R20).
//
// NO se modificó app.js: aquí se capturan sus funciones globales (entrar y
// salir) y se les añade el comportamiento vivo. Si este archivo no carga, la
// aplicación funciona igual pero sin avisos en vivo.
const Eventos = (() => {
  let fuente = null;
  // Misma clave de sessionStorage que usa api.js para el token.
  const CLAVE_TOKEN = "trazared_token";

  function token() {
    try { return sessionStorage.getItem(CLAVE_TOKEN); } catch { return null; }
  }

  function iniciar() {
    detener();
    const t = token();
    if (!t) return;
    // EventSource no envía cabeceras: el token viaja como ?token= (R17).
    // El navegador reconecta solo si la conexión cae.
    fuente = new EventSource(`/api/eventos/stream?token=${encodeURIComponent(t)}`);
    fuente.onmessage = (m) => {
      let ev;
      try { ev = JSON.parse(m.data); } catch { return; }
      // Para que cada pantalla haga lo suyo con el mismo evento.
      document.dispatchEvent(new CustomEvent("trazared:evento", { detail: ev }));
      if (window.Notificaciones) Notificaciones.refrescar();
      if (/alerta|cr[ií]tico/i.test(ev.tipo || "")) {
        if (window.Alertas) Alertas.refrescar();
        if (ev.tipo !== "conectado") toast(ev.mensaje || "Alerta crítica", "error");
      }
    };
    fuente.onerror = () => { /* reconexión automática del navegador */ };
  }

  function detener() {
    if (fuente) { fuente.close(); fuente = null; }
  }

  // true si el canal SSE está abierto (lo usa Notificaciones para re-engancharse).
  function conectado() {
    return !!fuente && fuente.readyState === EventSource.OPEN;
  }

  return { iniciar, detener, token, conectado };
})();

// Se expone en window para poder inspeccionarlo desde la consola (const no se
// publica solo).
window.Eventos = Eventos;

// El arranque de app.js llama a entrar() con el token guardado ANTES de que
// este archivo cargue (auto-login); si ya hay sesión, abrimos el canal aquí.
setTimeout(() => {
  if (typeof sesion !== "undefined" && sesion.usuario) Eventos.iniciar();
}, 1500);

// ── Enganches con app.js (sin modificarlo) ────────────────────────────────
const _entrarBase = window.entrar;
window.entrar = async (...args) => {
  const r = await _entrarBase(...args);
  Eventos.iniciar();               // R17: el canal se abre al iniciar sesión
  if (window.Alertas) Alertas.refrescar();   // alertas que ya estaban activas
  return r;
};

const _salirBase = window.salir;
window.salir = (...args) => {
  Eventos.detener();
  return _salirBase(...args);
};
