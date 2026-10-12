// ==========================================================================
// TrazaRed HUV — alertas críticas con reconocimiento (R20, Persona 4)
// ==========================================================================
// Cuando el modelo clasifica un caso como crítico aparece un aviso PERSISTENTE
// (no se cierra solo) hasta que un clínico lo reconoce con una nota. Paciente
// y contable ni lo ven: el API responde 403 a su lista de alertas.
//
// NO se modificó app.js: este módulo solo pinta en #alertas-contenedor
// (inyectado en index.html). Los avisos en vivo llegan por eventos.js (R17).
const Alertas = (() => {
  const CLINICOS = ["admin", "medico", "especialista"];

  async function refrescar() {
    if (typeof sesion === "undefined" || !sesion.usuario) return;
    if (!CLINICOS.includes(sesion.usuario.rol)) return;
    let lista = [];
    try { lista = await API.get("/alertas"); } catch { return; }
    const cont = $("#alertas-contenedor");
    if (!cont) return;
    const activas = lista.filter(a => a.estado === "activa");
    if (!activas.length) { cont.innerHTML = ""; return; }
    // Aviso persistente compacto: la barra se ve siempre y el detalle se
    // despliega al hacer clic (R20: visible hasta que el clínico la reconoce).
    const ultima = activas[0];
    cont.innerHTML = `
      <div class="alertas-panel">
        <button class="alertas-toggle" aria-expanded="false">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4"/><circle cx="12" cy="17" r=".5"/></svg>
          <strong>${activas.length} alerta${activas.length > 1 ? "s" : ""} crítica${activas.length > 1 ? "s" : ""} sin reconocer</strong>
          <span class="alertas-resumen">${esc(ultima.mensaje || "")}</span>
          <span class="alertas-flecha" aria-hidden="true">▾</span>
        </button>
        <div class="alertas-lista oculto">
          ${activas.map(a => `
            <div class="alerta-critica" role="alert" data-alerta="${a.id}">
              <div class="alerta-cuerpo">
                <strong>Alerta crítica${a.paciente_id ? ` · Paciente ${a.paciente_id}` : ""} · ${esc(a.nivel || "critico")}</strong>
                <p>${esc(a.mensaje || "")}</p>
                <div class="alerta-acciones">
                  <input type="text" class="alerta-nota" placeholder="Nota del reconocimiento" aria-label="Nota del reconocimiento">
                  <button class="btn-reconocer">Reconocer</button>
                </div>
              </div>
            </div>`).join("")}
        </div>
      </div>`;
    cont.querySelector(".alertas-toggle").addEventListener("click", (e) => {
      const listaEl = cont.querySelector(".alertas-lista");
      const abierto = !listaEl.classList.toggle("oculto");
      e.currentTarget.setAttribute("aria-expanded", String(abierto));
    });
    cont.querySelectorAll(".btn-reconocer").forEach(btn =>
      btn.addEventListener("click", () => reconocer(btn.closest("[data-alerta]"))));
  }

  async function reconocer(bloque) {
    const id = +bloque.dataset.alerta;
    const nota = bloque.querySelector(".alerta-nota").value.trim();
    try {
      await API.post(`/alertas/${id}/reconocer`, { nota });
      toast("Alerta reconocida y registrada en la auditoría", "ok");
      await refrescar();
      if (window.Notificaciones) Notificaciones.refrescar();
    } catch (e) {
      toast(e.message || "No se pudo reconocer la alerta", "error");
    }
  }

  return { refrescar };
})();

// Se expone en window para que eventos.js lo encuentre (const no se publica solo).
window.Alertas = Alertas;

// Al entrar se pintan las alertas que YA estén activas (no solo las nuevas).
setTimeout(() => {
  if (typeof sesion !== "undefined" && sesion.usuario) Alertas.refrescar();
}, 1500);
