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
  let conexion = null;
  let conectado = false;
  let reconexion = null;

  async function iniciar() {
    detener();
    const token = sessionStorage.getItem("trazared_token");
    if (!token) return;
    const controlador = new AbortController();
    conexion = controlador;
    try {
      const respuesta = await fetch(API.ruta("/eventos/stream"), {
        headers: { Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
        signal: controlador.signal,
      });
      if (respuesta.status === 401 || respuesta.status === 403) {
        controlador.abort();
        return;
      }
      if (!respuesta.ok || !respuesta.body) throw new Error("Canal de eventos no disponible");
      if (controlador.signal.aborted) return;
      conectado = true;
      const lector = respuesta.body.pipeThrough(new TextDecoderStream()).getReader();
      let pendiente = "";
      while (true) {
        const { done, value } = await lector.read();
        if (done) break;
        pendiente += value;
        const mensajes = pendiente.split("\n\n");
        pendiente = mensajes.pop();
        for (const mensaje of mensajes) {
          if (controlador.signal.aborted) break;
          if (!mensaje.startsWith("data: ")) continue;
          let ev;
          try { ev = JSON.parse(mensaje.slice(6)); } catch { continue; }
          document.dispatchEvent(new CustomEvent("trazared:evento", { detail: ev }));
          if (window.Notificaciones) Notificaciones.refrescar();
          if (/alerta|cr[ií]tico/i.test(ev.tipo || "")) {
            if (window.Alertas) Alertas.refrescar();
            toast(ev.mensaje || "Alerta crítica", "error");
          }
        }
      }
    } catch (error) {
      if (!controlador.signal.aborted) console.debug("Se reconectará el canal de eventos", error);
    } finally {
      if (conexion === controlador) {
        conectado = false;
        conexion = null;
        if (sessionStorage.getItem("trazared_token") === token && !controlador.signal.aborted)
          reconexion = setTimeout(iniciar, 3000);
      }
    }
  }

  function detener() {
    clearTimeout(reconexion);
    conexion?.abort();
    conexion = null;
    conectado = false;
  }

  return { iniciar, detener, conectado: () => conectado };
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
