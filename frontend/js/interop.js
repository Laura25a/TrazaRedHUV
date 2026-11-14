// ==========================================================================
// TrazaRed HUV — Interoperabilidad con la institución socia (R30 a R33)
// ==========================================================================
// Una sola pantalla con lo que cada rol necesita:
//   · Especialista: RECIBIDAS (lo que llegó de otra institución) → aceptar / rechazar.
//   · Médico:       ENVIADAS (lo que remitió y qué respondieron).
//   · Admin:        además SOCIOS (credenciales de sistema) y MENSAJES (bitácora técnica).
// El envío se hace desde el formato de la remisión con el botón "Remitir a
// institución socia": abrirEnvioSocio() revisa el consentimiento del paciente,
// muestra qué viaja (y qué NO viaja) y envía el Bundle FHIR.
// Las bandejas se refrescan solas cuando llega un evento en vivo (SSE).
"use strict";

const ITP = { pestana: null };
const ITP_ESTADO = { pendiente: "est-pendiente", en_gestion: "est-pendiente", aceptada: "est-activo", rechazada: "est-inactivo", completada: "est-activo" };

async function vistaInterop(params = {}) {
  const rol = sesion.usuario.rol;
  const pestanas = [["recibidas", "Recibidas"], ["enviadas", "Enviadas"],
    ...(rol === "admin" ? [["socios", "Instituciones socias"], ["mensajes", "Mensajes (bitácora)"]] : [])];
  ITP.pestana = params.pestana || ITP.pestana || (rol === "medico" ? "enviadas" : "recibidas");
  if (!pestanas.some(([k]) => k === ITP.pestana)) ITP.pestana = "recibidas";
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Interoperabilidad</h2>
      <p>Remisiones que cruzan a otra institución en FHIR, de sistema a sistema: con token de sistema, firma en cada
      mensaje, consentimiento del paciente y solo los datos necesarios.</p></div></div>
    <section class="tarjeta"><div class="pestanas" id="itp-pestanas">${pestanas.map(([k, t]) =>
      `<button data-p="${k}" class="${k === ITP.pestana ? "activa" : ""}">${t}</button>`).join("")}</div>
      <div id="itp-cuerpo" class="itp-cuerpo">${cargando()}</div></section></div>`;
  $$("#itp-pestanas button").forEach(b => b.addEventListener("click", () => { ITP.pestana = b.dataset.p; vistaInterop(); }));
  try {
    await ({ recibidas: itpRecibidas, enviadas: itpEnviadas, socios: itpSocios, mensajes: itpMensajes })[ITP.pestana]();
  } catch (e) { $("#itp-cuerpo").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; }
}

const itpMotivo = (m) => esc(String(m || "").replace(/ \(propuesta del asistente.*$/, ""));

// ---------- Recibidas: la bandeja del especialista ----------
async function itpRecibidas() {
  const filas = await API.get("/interop/recibidas");
  const puede = ["especialista", "admin"].includes(sesion.usuario.rol);
  $("#itp-cuerpo").innerHTML = filas.length ? filas.map(r => {
    const x = r.recurso || {};
    const abierta = ["pendiente", "en_gestion"].includes(r.estado);
    return `<article class="itp-tarjeta ${abierta ? "abierta" : ""}">
      <header><div><strong>${esc(r.paciente_nombre)}</strong>${r.por_conciliar ? ` <span class="pildora est-pendiente" title="El paciente llegó con identificador federado y aún no se ha unido a un paciente de esta institución">Por conciliar</span>` : ""}
        <span class="tenue">desde ${esc(r.institucion_origen)} · ${esc(r.referencia_externa || "")} · ${fechaHora(r.recibida_en)}</span></div>
        <span class="pildora ${ITP_ESTADO[r.estado] || ""}">${esc(ESTADOS[r.estado] || r.estado)}</span></header>
      <p class="itp-motivo">${itpMotivo(r.motivo)}</p>
      ${(x.observaciones || []).length ? `<div class="itp-obs">${x.observaciones.map(o =>
        `<span title="LOINC ${esc(o.loinc)}">${esc(o.nombre)} <strong>${esc(o.valor)}</strong> ${esc(o.unidad || "")}</span>`).join("")}</div>` : ""}
      ${x.reporte_ia ? `<p class="itp-ia">${icono("ia")} <span><strong>Reporte de IA aprobado en origen.</strong> ${esc(x.reporte_ia.conclusion)}</span></p>` : ""}
      <footer><button class="btn btn-suave btn-sm" data-ver="${r.id}">Ver el formato</button>
        ${abierta && puede ? `<span class="itp-acciones"><input type="text" maxlength="300" placeholder="Nota (obligatoria si rechazas)" data-nota="${r.id}">
          <button class="btn btn-ok btn-sm" data-resp="aceptar" data-id="${r.id}">Aceptar</button>
          <button class="btn btn-peligro btn-sm" data-resp="rechazar" data-id="${r.id}">Rechazar</button></span>`
        : r.estado_interop === "respondida" ? `<span class="tenue">Respuesta entregada a ${esc(r.institucion_origen)}</span>`
        : !abierta && puede ? `<button class="btn btn-secundario btn-sm" data-resp="${r.estado === "aceptada" ? "aceptar" : "rechazar"}" data-id="${r.id}" data-reenviar="1">Reenviar la respuesta</button>` : ""}</footer>
    </article>`;
  }).join("") : `<div class="vacio">Aún no ha llegado ninguna remisión de otra institución.</div>`;
  $$("#itp-cuerpo [data-ver]").forEach(b => b.addEventListener("click", () => ir("ficha", { remision: +b.dataset.ver, desde: "interop" })));
  $$("#itp-cuerpo [data-resp]").forEach(b => b.addEventListener("click", async () => {
    const nota = ($(`[data-nota="${b.dataset.id}"]`) || {}).value || (b.dataset.reenviar ? "Reenvío de la respuesta" : "");
    if (b.dataset.resp === "rechazar" && !nota.trim()) { toast("Escribe el motivo del rechazo.", "error"); return; }
    b.disabled = true;
    try {
      const r = await API.post(`/interop/recibidas/${b.dataset.id}/responder`, { decision: b.dataset.resp, nota: nota.trim() || null });
      toast(r.respuesta_enviada ? `Remisión ${r.estado}: la otra institución ya recibió la respuesta.`
        : `Remisión ${r.estado}, pero la respuesta no llegó al socio: ${r.detalle}`, r.respuesta_enviada ? "ok" : "error");
      remisionesCache = null;
      itpRecibidas();
    } catch (e) { toast(e.message, "error"); b.disabled = false; }
  }));
}

// ---------- Enviadas ----------
async function itpEnviadas() {
  const filas = await API.get("/interop/enviadas");
  $("#itp-cuerpo").innerHTML = filas.length ? `<div class="tabla-scroll"><table><thead><tr><th>Paciente</th><th>Institución socia</th>
    <th>Motivo</th><th>Enviada</th><th>Respuesta</th></tr></thead><tbody>${filas.map(r => `<tr class="clic" data-ver="${r.id}">
      <td>${esc(r.paciente_nombre)}</td><td>${esc(r.socio)}</td><td>${itpMotivo(r.motivo).slice(0, 90)}</td>
      <td>${r.enviada_en ? fechaHora(r.enviada_en) : "—"}<span class="sub">${r.codigo_http === 202 ? "recibida · paciente por conciliar allá" : r.codigo_http === 201 ? "recibida y asociada" : ""}</span></td>
      <td><span class="pildora ${ITP_ESTADO[r.estado] || ""}">${r.estado_interop === "respondida" ? esc(ESTADOS[r.estado]) : "Esperando respuesta"}</span>
        ${r.nota_respuesta ? `<span class="sub">${esc(r.nota_respuesta)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="vacio">No has remitido a ninguna institución socia todavía.<br>Abre una remisión en el libro y usa <strong>Remitir a institución socia</strong>.</div>`;
  $$("#itp-cuerpo [data-ver]").forEach(tr => tr.addEventListener("click", () => ir("ficha", { remision: +tr.dataset.ver, desde: "interop" })));
}

// ---------- Socios (admin) ----------
async function itpSocios() {
  const socios = await API.get("/interop/socios");
  $("#itp-cuerpo").innerHTML = `
    <div class="itp-barra"><button class="btn btn-primario btn-sm" id="itp-nuevo">+ Registrar institución socia</button>
      <button class="btn btn-secundario btn-sm" id="itp-prueba" title="Este mismo sistema hace de socio: sirve para ensayar el flujo completo sin el otro equipo">Crear socio de prueba (bucle local)</button>
      <a class="btn btn-suave btn-sm" href="${API.BASE}/interop/perfil" target="_blank" rel="noopener">Ver el contrato (JSON)</a></div>
    ${socios.length ? socios.map(s => `<article class="itp-tarjeta">
      <header><div><strong>${esc(s.nombre)}</strong>${s.de_prueba ? ` <span class="pildora est-pendiente">de prueba</span>` : ""}
        <span class="tenue">client_id <code>${esc(s.client_id)}</code> · permisos ${esc(s.scopes.join(", "))}</span></div>
        <span class="pildora ${s.activo ? "est-activo" : "est-inactivo"}">${s.activo ? "Activo" : "Desactivado"}</span></header>
      <p class="itp-motivo">${s.listo_para_enviar ? `Listo para enviar a <code>${esc(s.url_base)}</code>.`
        : `Para <strong>enviarle</strong> remisiones falta ${[!s.url_base && "su URL", !s.cliente_saliente && "el client_id que nos dio", !s.tiene_secreto_saliente && "su client_secret", !s.tiene_firma_saliente && "su secreto de firma"].filter(Boolean).join(", ")}.`}</p>
      <footer><button class="btn btn-suave btn-sm" data-conf="${s.id}">Configurar envío</button>
        <button class="btn btn-suave btn-sm" data-probar="${s.id}" ${s.listo_para_enviar ? "" : "disabled"}>Probar conexión</button>
        <button class="btn btn-secundario btn-sm" data-rotar="${s.id}">Rotar credenciales</button>
        <button class="btn btn-secundario btn-sm" data-activo="${s.id}" data-v="${s.activo ? 0 : 1}">${s.activo ? "Desactivar" : "Activar"}</button></footer>
    </article>`).join("") : `<div class="vacio">Sin instituciones socias. Registra la del equipo pareja o crea el socio de prueba.</div>`}
    <p class="tenue itp-pie">Al registrar un socio se generan <strong>sus</strong> credenciales para entrar a este sistema (se muestran una sola vez).
      Las que <strong>ellos</strong> les entreguen se guardan cifradas. Intercámbienlas por un canal privado, nunca por el repositorio.</p>`;
  const mostrar = (r) => {
    const c = r.credenciales_para_entregar;
    abrirFormulario({ titulo: `Credenciales para ${r.nombre}`, textoBoton: "Ya las copié", alGuardar: async () => { itpSocios(); },
      campos: `<p class="itp-aviso">${icono("alerta")} ${esc(r.aviso)}</p>
        <label>client_id<input readonly value="${esc(c.client_id)}"></label>
        <label>client_secret<input readonly value="${esc(c.client_secret)}"></label>
        <label>secreto de firma (HMAC)<input readonly value="${esc(c.secreto_firma)}"></label>
        <p class="tenue">Con esto el socio pide su token en <code>/api/oauth/token</code> y envía a <code>/api/interop/Bundle</code>, <code>/Task</code> y <code>/Observation</code>.</p>`,
      alAbrir: (f) => $$("input[readonly]", f).forEach(i => i.addEventListener("focus", () => i.select())) });
  };
  $("#itp-nuevo").addEventListener("click", () => abrirFormulario({ titulo: "Registrar institución socia", textoBoton: "Registrar y generar credenciales",
    campos: `<label>Nombre de la institución<input name="nombre" required minlength="3" maxlength="120" placeholder="Ej.: Clínica del equipo 3"></label>
      <label>URL de su API (opcional por ahora)<input name="url_base" placeholder="https://su-tunel.trycloudflare.com/api"></label>`,
    alGuardar: async (d) => mostrar(await API.post("/interop/socios", { nombre: d.nombre, url_base: d.url_base || null })) }));
  $("#itp-prueba").addEventListener("click", async () => {
    try { const r = await API.post("/interop/socios/prueba"); toast(r.ya_existia ? "El socio de prueba ya existía." : "Socio de prueba creado: ya puedes ensayar el envío.", "ok"); itpSocios(); }
    catch (e) { toast(e.message, "error"); }
  });
  $$("#itp-cuerpo [data-conf]").forEach(b => b.addEventListener("click", () => {
    const s = socios.find(x => x.id === +b.dataset.conf);
    abrirFormulario({ titulo: `Enviar a ${s.nombre}`, textoBoton: "Guardar",
      campos: `<p class="tenue">Datos que <strong>${esc(s.nombre)}</strong> les entregó para entrar a su sistema. Los secretos se guardan cifrados; deja un campo vacío para conservar el actual.</p>
        <label>URL de su API<input name="url_base" value="${esc(s.url_base || "")}" placeholder="https://su-tunel.trycloudflare.com/api"></label>
        <label>client_id que nos dieron<input name="cliente_saliente" value="${esc(s.cliente_saliente || "")}"></label>
        <label>client_secret que nos dieron<input name="secreto_saliente" type="password" autocomplete="new-password" placeholder="${s.tiene_secreto_saliente ? "(guardado)" : ""}"></label>
        <label>secreto de firma que nos dieron<input name="firma_saliente" type="password" autocomplete="new-password" placeholder="${s.tiene_firma_saliente ? "(guardado)" : ""}"></label>`,
      alGuardar: async (d) => { await API.put(`/interop/socios/${s.id}`, Object.fromEntries(Object.entries(d).filter(([k, v]) => v !== "" || k === "url_base").map(([k, v]) => [k, v || null]))); toast("Configuración guardada.", "ok"); itpSocios(); } });
  }));
  $$("#itp-cuerpo [data-probar]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true;
    try { const r = await API.post(`/interop/socios/${b.dataset.probar}/probar`); toast(r.detalle, r.ok ? "ok" : "error"); }
    catch (e) { toast(e.message, "error"); } finally { b.disabled = false; }
  }));
  $$("#itp-cuerpo [data-rotar]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Se generan credenciales nuevas y las anteriores dejan de servir de inmediato. ¿Continuar?")) return;
    try { mostrar(await API.post(`/interop/socios/${b.dataset.rotar}/rotar`)); } catch (e) { toast(e.message, "error"); }
  }));
  $$("#itp-cuerpo [data-activo]").forEach(b => b.addEventListener("click", async () => {
    try { await API.put(`/interop/socios/${b.dataset.activo}`, { activo: b.dataset.v === "1" }); itpSocios(); } catch (e) { toast(e.message, "error"); }
  }));
}

// ---------- Mensajes (admin): cada cosa que entró o salió, con su código ----------
async function itpMensajes() {
  const filas = await API.get("/interop/mensajes?limite=150");
  const tono = (c) => !c ? "est-inactivo" : c < 300 ? "est-activo" : c === 409 ? "est-pendiente" : "est-inactivo";
  $("#itp-cuerpo").innerHTML = filas.length ? `<div class="tabla-scroll"><table><thead><tr><th>Cuándo</th><th></th><th>Socio</th><th>Recurso</th>
    <th>HTTP</th><th>Estado</th><th>Detalle</th></tr></thead><tbody>${filas.map(m => `<tr>
      <td class="num">${fechaHora(m.creado_en)}</td><td>${m.direccion === "entrante" ? "⇣ entró" : "⇡ salió"}</td><td>${esc(m.socio || "—")}</td>
      <td>${esc(m.tipo)}${m.remision_id ? `<span class="sub">remisión ${m.remision_id}</span>` : ""}</td>
      <td><span class="pildora ${tono(m.codigo_http)}">${m.codigo_http || "sin conexión"}</span></td><td>${esc(m.estado.replace("_", " "))}</td>
      <td class="itp-detalle">${esc(m.detalle || m.referencia_externa || "")}<span class="sub"><code>${esc(String(m.mensaje_id).slice(0, 18))}…</code></span></td></tr>`).join("")}</tbody></table></div>`
    : `<div class="vacio">Sin mensajes todavía.</div>`;
}

// ---------- Enviar una remisión a la institución socia (desde el formato) ----------
async function abrirEnvioSocio(remisionId, pacienteId, recargar) {
  let socios;
  try { socios = (await API.get("/interop/socios")).filter(s => s.activo); } catch (e) { toast(e.message, "error"); return; }
  if (!socios.length) { toast("No hay instituciones socias registradas. El administrador las crea en Interoperabilidad.", "info"); return; }
  const fondo = document.createElement("div");
  fondo.className = "fondo-modal";
  fondo.innerHTML = `<div class="modal modal-itp" role="dialog" aria-label="Remitir a institución socia">
    <div class="modal-cabeza"><h3>Remitir a institución socia</h3><button type="button" class="btn-icono" data-cerrar aria-label="Cerrar">${icono("x")}</button></div>
    <div class="modal-cuerpo">
      <label>Institución<select id="itp-socio">${socios.map(s => `<option value="${s.id}">${esc(s.nombre)}${s.listo_para_enviar ? "" : " (falta configurar el envío)"}</option>`).join("")}</select></label>
      <div id="itp-previa">${cargando("Revisando el consentimiento…")}</div></div>
    <div class="modal-pie"><button type="button" class="btn btn-secundario" data-cerrar>Cancelar</button>
      <button type="button" class="btn btn-primario" id="itp-enviar" disabled>Enviar remisión (FHIR)</button></div></div>`;
  document.body.appendChild(fondo);
  fondo.addEventListener("mousedown", (e) => { if (e.target === fondo) fondo.remove(); });
  $$("[data-cerrar]", fondo).forEach(b => b.addEventListener("click", () => fondo.remove()));
  const socio = () => +$("#itp-socio", fondo).value;
  const revisar = async () => {
    const caja = $("#itp-previa", fondo), btn = $("#itp-enviar", fondo);
    btn.disabled = true;
    caja.innerHTML = cargando("Revisando…");
    try {
      const v = await API.get(`/interop/remisiones/${remisionId}/vista-previa?socio_id=${socio()}`);
      const recursos = Object.entries(v.recursos).map(([t, n]) => `<li><strong>${n}</strong> ${esc({ Patient: "paciente (con identificador federado, sin documento)", ServiceRequest: "solicitud de remisión", Observation: "signos vitales (LOINC)", DiagnosticReport: "reporte de IA aprobado por un clínico", Consent: "consentimiento del paciente" }[t] || t)}</li>`).join("");
      caja.innerHTML = `${v.consentimiento
        ? `<p class="itp-ok">${icono("check")} El paciente autorizó compartir sus datos con ${esc(v.socio)}.</p>`
        : `<div class="itp-aviso">${icono("alerta")} <div>El paciente <strong>no tiene consentimiento registrado</strong> para ${esc(v.socio)}. Sin él no se puede remitir (Ley 1581 de 2012).
            <div class="itp-consent"><select id="itp-medio"><option value="escrito">Firmó la autorización (escrito)</option><option value="verbal">Autorizó verbalmente</option><option value="digital">Autorizó por medio digital</option></select>
            <button type="button" class="btn btn-ok btn-sm" id="itp-registrar">Registrar consentimiento</button></div></div></div>`}
        <p class="itp-sub">Viaja en el mensaje</p><ul class="itp-lista">${recursos}${v.reporte_ia_aprobado ? "" : `<li class="tenue">Sin reporte de IA: solo se envía si un clínico lo aprobó.</li>`}</ul>
        <p class="itp-sub">No sale de este hospital</p><p class="tenue">${esc(v.no_viaja.join(" · "))}</p>
        <p class="tenue">El asiento ${esc(v.radicado)} quedará con destino ${esc(v.socio)} y pasará a "en gestión".</p>`;
      btn.disabled = !v.consentimiento;
      const reg = $("#itp-registrar", fondo);
      if (reg) reg.addEventListener("click", async () => {
        reg.disabled = true;
        try { await API.post(`/pacientes/${pacienteId}/consentimientos`, { socio_id: socio(), medio: $("#itp-medio", fondo).value }); toast("Consentimiento registrado.", "ok"); revisar(); }
        catch (e) { toast(e.message, "error"); reg.disabled = false; }
      });
    } catch (e) { caja.innerHTML = `<p class="error-form">${esc(e.message)}</p>`; }
  };
  $("#itp-socio", fondo).addEventListener("change", revisar);
  $("#itp-enviar", fondo).addEventListener("click", async () => {
    const btn = $("#itp-enviar", fondo);
    btn.disabled = true; btn.textContent = "Enviando…";
    try {
      const r = await API.post(`/interop/remisiones/${remisionId}/enviar`, { socio_id: socio() });
      toast(`Remisión enviada a ${r.socio}: ${r.paciente_conciliado ? "recibida y asociada a su paciente" : "recibida; allá queda por conciliar el paciente"}.`, "ok");
      remisionesCache = null;
      fondo.remove();
      if (recargar) recargar();
    } catch (e) { toast(e.message, "error"); btn.disabled = false; btn.textContent = "Enviar remisión (FHIR)"; }
  });
  revisar();
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.interop = '<path d="M7 7h11l-3-3M17 17H6l3 3"/><circle cx="5" cy="7" r="1.6"/><circle cx="19" cy="17" r="1.6"/>';
  SECCIONES.interop = { titulo: "Interoperabilidad", icono: "interop", roles: ["admin", "medico", "especialista"],
    pintar: vistaInterop, grupo: "Red y seguridad" };
}

// En vivo (R33): cuando llega una remisión del socio o su respuesta, la pantalla
// abierta se actualiza sola, sin recargar.
document.addEventListener("trazared:evento", (e) => {
  const ev = e.detail || {};
  if (!["remision_externa_recibida", "respuesta_socio", "interop_rechazo"].includes(ev.tipo)) return;
  toast(ev.mensaje, ev.tipo === "interop_rechazo" ? "error" : "ok");
  if (typeof remisionesCache !== "undefined") remisionesCache = null;
  if (sesion.usuario && sesion.seccion === "interop") vistaInterop();
});
