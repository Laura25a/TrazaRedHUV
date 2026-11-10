// ==========================================================================
// TrazaRed HUV — Recuperación de información e Ingeniería biomédica
// ==========================================================================
// Nada se destruye (Res. 1995 de 1999): eliminar es un borrado lógico y se
// puede deshacer. Quién hace qué:
//   médico, admin  → eliminan una imagen desde la ficha (motivo obligatorio)
//   admin          → recupera imágenes y remisiones
//   biomédico      → recupera IMÁGENES, ve el estado técnico y la auditoría técnica
//                    (los pacientes le aparecen con seudónimo; no ve fichas)
// Rutas: POST /imagenes/{id}/eliminar · GET /recuperacion/imagenes ·
//        POST /recuperacion/imagenes/{id}/restaurar · GET /tecnico/estado · GET /tecnico/auditoria
"use strict";

const REC = { pestana: "imagenes", urls: [] };

// ---------- Eliminar desde la galería de la ficha ----------
function activarEliminarImagen(raiz, alTerminar) {
  $$("[data-eliminar]", raiz).forEach(caja => {
    const id = caja.dataset.eliminar;
    $("button", caja).addEventListener("click", () => {
      caja.innerHTML = `<input type="text" class="rec-motivo" maxlength="300" placeholder="Motivo (ej.: se subió a otro paciente)">
        <div class="rec-botones"><button type="button" class="btn btn-peligro btn-sm" data-ok>Eliminar</button>
        <button type="button" class="btn btn-suave btn-sm" data-no>Cancelar</button></div>`;
      $(".rec-motivo", caja).focus();
      $("[data-no]", caja).addEventListener("click", alTerminar);
      $("[data-ok]", caja).addEventListener("click", async () => {
        const motivo = $(".rec-motivo", caja).value.trim();
        if (motivo.length < 5) { toast("Escribe el motivo (mínimo 5 caracteres).", "error"); return; }
        $$("button, input", caja).forEach(x => x.disabled = true);
        try {
          await API.post(`/imagenes/${id}/eliminar`, { motivo });
          toast("Imagen eliminada. Sigue en el PACS: el admin o Ingeniería biomédica la pueden recuperar.", "ok");
          alTerminar();
        } catch (e) { toast(e.message, "error"); $$("button, input", caja).forEach(x => x.disabled = false); }
      });
    });
  });
}

// ---------- Papelera / recuperación ----------
function recLiberar() { REC.urls.forEach(u => URL.revokeObjectURL(u)); REC.urls = []; }
const recFecha = (v) => (v ? String(v).replace("T", " ").slice(0, 16) : "—");

async function vistaRecuperacion() {
  recLiberar();
  const admin = sesion.usuario.rol === "admin";
  if (!admin) REC.pestana = REC.pestana === "remisiones" ? "imagenes" : REC.pestana;
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Recuperación de información</h2>
      <p>Lo eliminado en TrazaRed no se destruye: se oculta y queda aquí, con quién lo eliminó y por qué.
      ${admin ? "Como administrador recuperas imágenes y remisiones." : "Como Ingeniería biomédica recuperas las imágenes del PACS."}</p></div></div>
    <section class="tarjeta">
      <div class="pestanas" id="rec-tabs">
        <button data-p="imagenes">Imágenes eliminadas</button>
        ${admin ? `<button data-p="remisiones">Remisiones eliminadas</button>` : ""}
        <button data-p="historial">Historial de recuperaciones</button>
      </div>
      <div class="tarjeta-cuerpo" id="rec-cuerpo">${cargando()}</div>
    </section></div>`;
  $$("#rec-tabs button").forEach(b => {
    b.classList.toggle("activa", b.dataset.p === REC.pestana);
    b.addEventListener("click", () => { REC.pestana = b.dataset.p; vistaRecuperacion(); });
  });
  const cuerpo = $("#rec-cuerpo");
  try {
    if (!cuerpo) return;
    if (REC.pestana === "imagenes") await recImagenes(cuerpo, admin);
    else if (REC.pestana === "remisiones") await recRemisiones(cuerpo);
    else await recHistorial(cuerpo, admin);
  } catch (e) {
    cuerpo.innerHTML = `<div class="vacio">${esc(e.status === 403 ? "Tu rol no tiene acceso a la recuperación." : e.message)}</div>`;
  }
}

function recFormRestaurar(clave) {
  return `<div class="rec-restaurar" data-restaurar="${esc(clave)}">
    <input type="text" class="rec-motivo" maxlength="300" placeholder="Motivo de la recuperación">
    <button type="button" class="btn btn-ok btn-sm">Restaurar</button></div>`;
}

async function recImagenes(cuerpo, admin) {
  const lista = await API.get("/recuperacion/imagenes");
  if (!lista.length) { cuerpo.innerHTML = `<div class="vacio">No hay imágenes eliminadas.</div>`; return; }
  cuerpo.innerHTML = `<div class="rec-grid">${lista.map(im => `<article class="rec-item">
      <div class="rec-img" data-img="${esc(im.instance_id)}"><div class="esqueleto" style="width:100%;height:100%"></div></div>
      <div class="rec-info">
        <strong>${esc(im.descripcion || "Imagen")}</strong>
        <small>${esc(im.modalidad || "")} · ${admin ? `${esc(im.paciente_nombre || "")} (#${im.paciente_id}) · ` : ""}paciente <code>${esc(im.paciente || "—")}</code></small>
        <p><b>Motivo:</b> ${esc(im.motivo)}</p>
        <small>Eliminada por ${esc(im.eliminada_por)} (${esc(ROLES[im.eliminada_por_rol] || im.eliminada_por_rol)}) · ${recFecha(im.eliminada_en)}</small>
        ${recFormRestaurar(im.instance_id)}
      </div></article>`).join("")}</div>
    <p class="tenue">El archivo DICOM nunca salió del PACS: restaurar solo vuelve a mostrarlo en la ficha del paciente.</p>`;
  lista.forEach(async im => {
    const cuadro = $(`[data-img="${im.instance_id}"]`, cuerpo);
    try {
      const url = URL.createObjectURL(await API.blob(`/recuperacion/imagenes/${im.instance_id}/preview`));
      REC.urls.push(url);
      if (cuadro) cuadro.innerHTML = `<img src="${url}" alt="${esc(im.descripcion || "Imagen eliminada")}">`;
    } catch { if (cuadro) cuadro.textContent = "Vista previa no disponible"; }
  });
  recActivarRestaurar(cuerpo, (id, motivo) => API.post(`/recuperacion/imagenes/${id}/restaurar`, { motivo }));
}

async function recRemisiones(cuerpo) {
  const lista = (await API.get("/remisiones?incluir_inactivas=true")).filter(r => r.activo === false);
  if (!lista.length) { cuerpo.innerHTML = `<div class="vacio">No hay remisiones eliminadas.</div>`; return; }
  cuerpo.innerHTML = `<div class="tabla-envoltura"><table class="rev-tabla"><thead><tr>
      <th>Remisión</th><th>Paciente</th><th>Destino</th><th>Motivo</th><th>Recuperar</th></tr></thead><tbody>
      ${lista.map(r => `<tr><td class="mono">#${r.id}</td><td>${esc(r.paciente_nombre || "#" + r.paciente_id)}</td>
        <td>${esc(r.institucion_destino)}</td><td>${esc(String(r.motivo || "").slice(0, 90))}</td>
        <td>${recFormRestaurar(r.id)}</td></tr>`).join("")}</tbody></table></div>`;
  recActivarRestaurar(cuerpo, (id, motivo) => API.post(`/remisiones/${id}/restaurar`, { motivo }));
}

async function recHistorial(cuerpo, admin) {
  const filas = (await API.get("/tecnico/auditoria?limite=200"))
    .filter(a => ["imagen_eliminada", "imagen_restaurada", "soft_delete", "restaurar"].includes(a.accion));
  if (!filas.length) { cuerpo.innerHTML = `<div class="vacio">Todavía no hay eliminaciones ni recuperaciones.</div>`; return; }
  const NOMBRE = { imagen_eliminada: "Imagen eliminada", imagen_restaurada: "Imagen restaurada", soft_delete: "Remisión eliminada", restaurar: "Remisión restaurada" };
  cuerpo.innerHTML = `<div class="tabla-envoltura"><table class="rev-tabla"><thead><tr><th>Fecha</th><th>Acción</th><th>Quién</th><th>Paciente</th><th>Detalle</th></tr></thead><tbody>
    ${filas.map(a => `<tr><td class="tenue">${recFecha(a.fecha)}</td>
      <td><span class="pildora ${a.accion.includes("restaur") ? "est-activo" : "est-pendiente"}">${esc(NOMBRE[a.accion] || a.accion)}</span></td>
      <td>${esc(a.usuario || "—")} <small class="tenue">${esc(ROLES[a.rol] || a.rol || "")}</small></td>
      <td><code>${esc(a.paciente || (admin && a.registro_id ? "#" + a.registro_id : "—"))}</code></td>
      <td>${esc(String(a.detalle || "").replace(/instancia [0-9a-f-]+ · /, ""))}</td></tr>`).join("")}</tbody></table></div>`;
}

function recActivarRestaurar(raiz, accion) {
  $$("[data-restaurar]", raiz).forEach(caja => {
    $("button", caja).addEventListener("click", async () => {
      const motivo = $(".rec-motivo", caja).value.trim();
      if (motivo.length < 5) { toast("Escribe el motivo de la recuperación (mínimo 5 caracteres).", "error"); $(".rec-motivo", caja).focus(); return; }
      $$("button, input", caja).forEach(x => x.disabled = true);
      try {
        await accion(caja.dataset.restaurar, motivo);
        caja.innerHTML = `<span class="pildora est-activo">Recuperada · queda en la auditoría</span>`;
        toast("Información recuperada.", "ok");
        remisionesCache = null;
      } catch (e) {
        $$("button, input", caja).forEach(x => x.disabled = false);
        toast(e.status === 403 ? "Tu rol no puede recuperar esto." : e.message, "error");
      }
    });
  });
}

// ---------- Estado técnico (Ingeniería biomédica) ----------
async function vistaTecnico() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Estado técnico</h2>
      <p>Servicios del sistema, PACS, modelo de IA y asistente. Ingeniería biomédica vigila que la tecnología funcione;
      no ve fichas ni datos clínicos.</p></div>
      <button class="btn btn-secundario" id="tec-recargar">Actualizar</button></div>
    <div id="tec-estado">${cargando("Revisando servicios…")}</div>
    <section class="tarjeta" style="margin-top:1.1rem"><div class="tarjeta-cuerpo">
      <h3 class="seccion-titulo">Auditoría técnica</h3>
      <p class="tenue">Imágenes subidas, eliminadas y recuperadas, reentrenamientos del modelo y bloqueos de cuentas.</p>
      <div id="tec-aud">${cargando()}</div></div></section></div>`;
  $("#tec-recargar").addEventListener("click", vistaTecnico);
  try {
    const e = await API.get("/tecnico/estado");
    if (!$("#tec-estado")) return;                 // el usuario ya cambió de pantalla
    $("#tec-estado").innerHTML = `<div class="tec-grid">${e.servicios.map(s => `<article class="tarjeta tec-servicio ${s.estado}">
        <span class="tec-punto"></span><div><strong>${esc(s.servicio)}</strong>
        <small>${esc(s.estado === "ok" ? (s.detalle || "funcionando") : (s.detalle || "sin respuesta"))}</small>
        ${s.pgvector ? `<small>pgvector: ${esc(s.pgvector)}</small>` : ""}${s.version ? `<small>versión ${esc(String(s.version))}</small>` : ""}</div></article>`).join("")}
      <article class="tarjeta tec-servicio ok"><span class="tec-punto"></span><div><strong>RAG (guías)</strong>
        <small>${esc(e.rag.modelo_embeddings || "")} · ${esc(e.rag.pgvector || "")}</small></div></article>
      <article class="tarjeta tec-servicio ${e.agente.error ? "error" : "ok"}"><span class="tec-punto"></span><div><strong>Asistente clínico</strong>
        <small>modelo ${esc(e.agente.proveedor || "")} · memoria ${esc(String(e.agente.memoria || "").split(" (")[0])}</small></div></article>
    </div>`;
  } catch (x) { if ($("#tec-estado")) $("#tec-estado").innerHTML = `<div class="tarjeta vacio">${esc(x.message)}</div>`; }
  try {
    const filas = await API.get("/tecnico/auditoria?limite=60");
    if (!$("#tec-aud")) return;
    $("#tec-aud").innerHTML = !filas.length ? `<div class="vacio">Sin eventos técnicos todavía.</div>` :
      `<div class="tabla-envoltura"><table class="rev-tabla"><thead><tr><th>Fecha</th><th>Acción</th><th>Quién</th><th>Paciente</th><th>Detalle</th></tr></thead><tbody>
      ${filas.map(a => `<tr><td class="tenue">${recFecha(a.fecha)}</td><td><code>${esc(a.accion)}</code></td>
        <td>${esc(a.usuario || "—")} <small class="tenue">${esc(ROLES[a.rol] || a.rol || "")}</small></td>
        <td><code>${esc(a.paciente || "—")}</code></td><td>${esc(String(a.detalle || "").slice(0, 120))}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (x) { if ($("#tec-aud")) $("#tec-aud").innerHTML = `<div class="vacio">${esc(x.message)}</div>`; }
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.recuperar = '<path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v4h4"/><path d="M12 8v4l3 2"/>';
  ICONOS.tecnico = '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/><path d="m7 10 2 2 3-4 2 3 3-2"/>';
  SECCIONES.recuperacion = { titulo: "Recuperación de información", icono: "recuperar", roles: ["admin", "biomedico"],
    pintar: vistaRecuperacion, grupo: "Gestión técnica" };
  SECCIONES.tecnico = { titulo: "Estado técnico", icono: "tecnico", roles: ["admin", "biomedico"],
    pintar: vistaTecnico, grupo: "Gestión técnica" };
  // Ingeniería biomédica entra directo a su estado técnico (no ve el libro de remisiones)
  const inicioAnterior = SECCIONES.inicio.pintar;
  SECCIONES.inicio.pintar = (p) => (sesion.usuario && sesion.usuario.rol === "biomedico" ? vistaTecnico(p) : inicioAnterior(p));
}
