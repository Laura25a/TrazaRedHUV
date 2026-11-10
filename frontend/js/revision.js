// ==========================================================================
// TrazaRed HUV — Revisión de reportes de IA (R10: "la IA sugiere, el médico decide")
// ==========================================================================
// Bandeja de reportes del modelo de urgencia:
//   GET  /ia/reportes?estado=pendiente|aprobado|rechazado
//   POST /ia/reportes/{id}/aprobar   {observaciones}   → entra a la historia y genera la factura
//   POST /ia/reportes/{id}/rechazar  {observaciones}   → NO entra a la historia; queda como evidencia
//   GET  /ia/reportes/{id}/payload                     → lo que exactamente recibió el modelo (seudonimizado)
// También exporta revisionBotones(), que la pantalla de predicción usa para
// aprobar o rechazar en el momento el reporte que acaba de crear.
"use strict";

const REV = { estado: "pendiente" };

async function vistaRevision() {
  const puedeDecidir = ["medico", "especialista"].includes(sesion.usuario.rol);
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Reportes de IA</h2>
      <p>Cada análisis del modelo nace <strong>pendiente</strong>. Solo entra a la historia clínica si un médico o
      especialista lo aprueba; lo rechazado no se borra: queda como evidencia.</p></div></div>
    <section class="tarjeta">
      <div class="pestanas" id="rev-tabs">
        ${["pendiente", "aprobado", "rechazado"].map(e => `<button data-e="${e}" class="${REV.estado === e ? "activa" : ""}">${e[0].toUpperCase() + e.slice(1)}s</button>`).join("")}
      </div>
      <div class="tarjeta-cuerpo" id="rev-lista">${cargando()}</div>
    </section>
    ${puedeDecidir ? "" : `<p class="tenue" style="margin-top:.8rem">Tu rol puede consultar los reportes; aprobar o rechazar es del médico o el especialista.</p>`}
  </div>`;
  $$("#rev-tabs button").forEach(b => b.addEventListener("click", () => { REV.estado = b.dataset.e; vistaRevision(); }));

  let lista;
  try { lista = await API.get(`/ia/reportes?estado=${REV.estado}`); }
  catch (e) { $("#rev-lista").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
  if (!lista.length) { $("#rev-lista").innerHTML = `<div class="vacio">No hay reportes ${REV.estado}s.</div>`; return; }

  $("#rev-lista").innerHTML = `<div class="tabla-envoltura"><table class="rev-tabla"><thead><tr>
      <th>#</th><th>Paciente</th><th>Probabilidad</th><th>Nivel</th><th>Fecha</th><th>${REV.estado === "pendiente" ? "Decisión" : "Observación"}</th></tr></thead>
    <tbody>${lista.slice(0, 80).map(r => `<tr data-id="${r.id}">
      <td class="mono">#${r.id}</td>
      <td><a href="#" data-pac="${r.paciente_id}">Paciente ${r.paciente_id}</a></td>
      <td><div class="rev-prob"><span>${agrNum(r.probabilidad * 100, 1)} %</span><i style="width:${Math.min(100, r.probabilidad * 100)}%"></i></div></td>
      <td><span class="pildora ${r.critico ? "est-pendiente" : r.nivel === "urgente" ? "est-pendiente" : "est-activo"}">${r.critico ? "Crítico" : r.nivel === "urgente" ? "Urgente" : "No urgente"}</span></td>
      <td class="tenue">${esc(String(r.fecha_creacion || "").replace("T", " ").slice(0, 16))}</td>
      <td>${REV.estado === "pendiente" && puedeDecidir ? revisionBotones(r.id) : esc(r.observaciones || "—")}</td></tr>`).join("")}
    </tbody></table></div>
    ${lista.length > 80 ? `<p class="tenue">Se muestran los 80 más recientes de ${lista.length}.</p>` : ""}`;
  $$("[data-pac]").forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); ir("asistente", { paciente: +a.dataset.pac }); }));
  activarRevision($("#rev-lista"), () => setTimeout(vistaRevision, 600));
}

// Botones reutilizables (también en la pantalla de predicción)
function revisionBotones(id) {
  return `<div class="rev-acciones" data-reporte="${id}">
    <input type="text" class="rev-obs" maxlength="300" placeholder="Observación clínica">
    <button class="btn btn-ok btn-sm" data-decision="aprobar">Aprobar</button>
    <button class="btn btn-peligro btn-sm" data-decision="rechazar">Rechazar</button>
    <button class="btn btn-suave btn-sm" data-decision="payload" title="Ver exactamente qué recibió el modelo">Qué vio la IA</button>
  </div>`;
}

function activarRevision(raiz, alTerminar) {
  $$(".rev-acciones", raiz).forEach(caja => {
    const id = caja.dataset.reporte;
    $$("button", caja).forEach(b => b.addEventListener("click", async () => {
      if (b.dataset.decision === "payload") {
        try {
          const p = await API.get(`/ia/reportes/${id}/payload`);
          toast("Payload seudonimizado: " + JSON.stringify(p.payload).slice(0, 260), "info");
        } catch (e) { toast(e.message, "error"); }
        return;
      }
      const obs = $(".rev-obs", caja).value.trim();
      if (b.dataset.decision === "rechazar" && !obs) { toast("Escribe por qué lo rechazas.", "error"); $(".rev-obs", caja).focus(); return; }
      $$("button, input", caja).forEach(x => x.disabled = true);
      try {
        await API.post(`/ia/reportes/${id}/${b.dataset.decision}`, { observaciones: obs || null });
        caja.innerHTML = `<span class="pildora ${b.dataset.decision === "aprobar" ? "est-activo" : "est-pendiente"}">${b.dataset.decision === "aprobar"
          ? "Aprobado: entra a la historia y se generó la factura" : "Rechazado: no entra a la historia"}</span>`;
        toast(b.dataset.decision === "aprobar" ? "Reporte aprobado." : "Reporte rechazado.", "ok");
        if (alTerminar) alTerminar();
      } catch (e) {
        $$("button, input", caja).forEach(x => x.disabled = false);
        toast(e.status === 403 ? "Solo el médico o el especialista deciden." : e.message, "error");
      }
    }));
  });
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.revision = '<path d="M9 4h6a1 1 0 0 1 1 1v1H8V5a1 1 0 0 1 1-1z"/><path d="M8 6H6a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V7a1 1 0 0 0-1-1h-2"/><path d="m9 14 2 2 4-4"/>';
  SECCIONES.revision = {
    titulo: "Reportes de IA", icono: "revision",
    roles: ["admin", "medico", "especialista"],
    pintar: vistaRevision, grupo: "Inteligencia artificial",
  };
}
