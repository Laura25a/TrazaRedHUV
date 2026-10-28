// ==========================================================================
// TrazaRed HUV — el sistema como REGISTRO de remisiones (rediseño)
// ==========================================================================
// Vistas nuevas (las llama SECCIONES de app.js):
//   vistaLibro          Libro de radicación: cada remisión es un asiento numerado
//                       con su recorrido, su último movimiento y el pronóstico IA.
//   vistaFormato        Formato oficial TR-FR-01 de una remisión: bitácora
//                       firmada, apoyo de IA, costos, cierre y recorrido completo.
//   vistaSeguimiento    El paciente ve su remisión como el rastreo de un envío.
//   vistaLibroContable  Lo que costó cada remisión y su cobro a la EPS.
//   vistaPronosticos    El modelo de IA de remisiones y cómo le ha ido.
// Todo lo que se anota es definitivo: nada se edita ni se borra en la bitácora.
"use strict";

// ---------------------------------------------------------------- utilidades
const MARCA = { x: "✕", o: "–", e: "?", v: "✓" };
const marcaHTML = (m, chica = false) => `<span class="marca-g ${esc(m.marca)}${chica ? " chica" : ""}" title="${esc(m.etiqueta || "")}">${MARCA[m.marca] || "·"}</span>`;
const SELLO = { pendiente: "PENDIENTE", en_gestion: "EN CURSO", aceptada: "ACEPTADA", rechazada: "RECHAZADA", completada: "TRASLADADO" };
const selloHTML = e => `<span class="sello s-${esc(e)}">${SELLO[e] || esc(e)}</span>`;
const pesos = v => `$ ${Math.round(Number(v) || 0).toLocaleString("es-CO")}`;
const pesosCortos = v => { const n = Number(v) || 0; return n >= 1e6 ? `$ ${(n / 1e6).toLocaleString("es-CO", { maximumFractionDigits: 1 })} M` : pesos(n); };

function horasTxt(h) {
  if (h == null) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${Math.floor(h)} h ${String(Math.round((h % 1) * 60)).padStart(2, "0")} min`;
  return `${(h / 24).toLocaleString("es-CO", { maximumFractionDigits: 1 })} días`;
}
function hace(iso) {
  if (!iso) return "";
  const h = (Date.now() - new Date(iso).getTime()) / 3600000;
  return h < 0 ? "" : `hace ${horasTxt(h)}`;
}
function partesFecha(iso) {                     // "28·10·26" y "09:31" en hora de Colombia
  if (!iso) return ["—", ""];
  const f = new Date(iso);
  const d = f.toLocaleDateString("es-CO", { timeZone: ZONA, day: "2-digit", month: "2-digit", year: "2-digit" }).replace(/\//g, "·");
  const h = f.toLocaleTimeString("es-CO", { timeZone: ZONA, hour: "2-digit", minute: "2-digit", hour12: false });
  return [d, h];
}
function claseIA(p) { return p >= 0.6 ? "ia-alta" : p >= 0.4 ? "ia-media" : "ia-baja"; }
function barraIA(p) {
  if (p == null) return `<span class="tenue">Sin pronóstico</span>`;
  const c = claseIA(p), pc = Math.round(100 * p);
  return `<div class="barra-ia"><span class="pista"><span class="${c}-bg" style="width:${pc}%"></span></span><strong class="${c}">${pc}%</strong></div>`;
}

async function descargar(ruta, nombre, boton) {
  const txt = boton ? boton.innerHTML : "";
  if (boton) { boton.disabled = true; boton.innerHTML = "Preparando…"; }
  try {
    const blob = await API.blob(ruta);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = nombre;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast(`Descargado: ${nombre} (queda en auditoría)`, "ok");
  } catch (e) { toast(e.message || "No se pudo descargar", "error"); }
  finally { if (boton) { boton.disabled = false; boton.innerHTML = txt; } }
}
function barraDescargas(lista) {
  return `<div class="descargas"><span>Descargar:</span>${lista.map((d, i) =>
    `<button type="button" class="btn btn-sm btn-descarga" data-descarga="${i}">${icono("descargar")}${esc(d.txt)}</button>`).join("")}</div>`;
}
function activarDescargas(raiz, lista) {
  $$("[data-descarga]", raiz).forEach(b => b.addEventListener("click", () => {
    const d = lista[+b.dataset.descarga];
    descargar(typeof d.ruta === "function" ? d.ruta() : d.ruta, typeof d.nombre === "function" ? d.nombre() : d.nombre, b);
  }));
}
const hoyArchivo = () => hoyISO().replace(/-/g, "");

function vistaInicioRegistro(params = {}) {
  const rol = sesion.usuario.rol;
  if (rol === "paciente") return vistaSeguimiento();
  if (rol === "contable") return vistaLibroContable(params);
  return vistaLibro(params);
}

// ==========================================================================
// LIBRO DE RADICACIÓN
// ==========================================================================
const estadoLibro = { vista: "abiertas", q: "", mes: "", inactivas: false, mostrar: 120 };

async function vistaLibro(params = {}) {
  if (params.vista) estadoLibro.vista = params.vista;
  if (params.q !== undefined) estadoLibro.q = params.q || "";
  estadoLibro.mostrar = 120;
  const u = sesion.usuario;
  const radica = ["admin", "medico"].includes(u.rol);
  const descargas = [
    { txt: "Excel", ruta: () => `/registro/libro.xlsx?vista=${estadoLibro.vista}${estadoLibro.q ? `&q=${encodeURIComponent(estadoLibro.q)}` : ""}`, nombre: () => `libro_radicacion_${hoyArchivo()}.xlsx` },
    { txt: "PDF del folio", ruta: () => `/registro/libro.pdf?vista=${estadoLibro.vista}${estadoLibro.q ? `&q=${encodeURIComponent(estadoLibro.q)}` : ""}`, nombre: () => `libro_radicacion_${hoyArchivo()}.pdf` },
    { txt: "CSV", ruta: () => `/registro/libro.csv?vista=${estadoLibro.vista}${estadoLibro.q ? `&q=${encodeURIComponent(estadoLibro.q)}` : ""}`, nombre: () => `libro_radicacion_${hoyArchivo()}.csv` },
  ];
  $("#contenido").innerHTML = `<div class="pagina">
    <div id="libro-kpis">${cargando("Abriendo el libro…")}</div>
    <div style="display:flex;justify-content:space-between;align-items:flex-end;gap:.8rem;flex-wrap:wrap">
      <div class="tomos" id="tomos"></div>
      <div style="display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;padding-bottom:.45rem">
        ${barraDescargas(descargas)}
        ${radica ? `<button class="btn btn-coral" id="btn-radicar">+ Radicar remisión</button>` : ""}
      </div>
    </div>
    <section class="hoja">
      <div class="hoja-cabeza">
        <div><div class="institucion">Hospital Universitario del Valle «Evaristo García»</div>
          <h2>Libro de radicación de remisiones</h2>
          ${u.rol === "eps" ? `<p class="suave">Asientos de los afiliados de ${esc(u.eps_nombre || "tu EPS")} · solo lectura</p>` : ""}</div>
        <div class="hoja-meta" id="hoja-meta"></div>
      </div>
      <div class="hoja-filtros">
        <div class="buscar">${icono("buscar")}<input type="search" id="l-q" aria-label="Buscar en el libro" placeholder="Buscar asiento, radicado, paciente, documento o institución…" value="${esc(estadoLibro.q)}"></div>
        <div id="chips" style="display:flex;gap:.4rem;flex-wrap:wrap"></div>
        ${u.rol === "admin" ? `<label class="check"><input type="checkbox" id="l-inact" ${estadoLibro.inactivas ? "checked" : ""}> Ver asientos anulados</label>` : ""}
      </div>
      <div class="tabla-scroll" id="l-tabla">${cargando()}</div>
      <div class="hoja-pie">
        <div class="leyenda-marcas">
          <span>${marcaHTML({ marca: "x" }, true)} No acepta</span><span>${marcaHTML({ marca: "o" }, true)} Sin respuesta</span>
          <span>${marcaHTML({ marca: "e" }, true)} En evaluación / pide documentos</span><span>${marcaHTML({ marca: "v" }, true)} Acepta</span>
        </div>
        <span class="mono">Los asientos no se tachan: toda corrección es un asiento nuevo que remite al original.</span>
      </div>
    </section></div>`;
  activarDescargas($("#contenido"), descargas);
  if (radica) $("#btn-radicar").addEventListener("click", () => formularioRemision());

  let datos;
  try {
    datos = await API.get(`/registro/libro?vista=todas&limite=2000${estadoLibro.inactivas ? "&incluir_inactivas=true" : ""}`);
  } catch (e) { $("#libro-kpis").innerHTML = ""; $("#l-tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
  const todos = datos.asientos;
  const r = datos.resumen;

  $("#libro-kpis").innerHTML = `<div class="kpis-libro">
    <div class="kpi-libro k-tinta"><small>Radicadas hoy</small><strong>${r.radicadas_hoy}</strong><span>desde las 00:00</span></div>
    <div class="kpi-libro"><small>Asientos abiertos</small><strong>${r.abiertas}</strong><span>sin gestión, en curso o aceptados</span></div>
    <div class="kpi-libro k-coral"><small>Sin movimiento &gt; ${r.umbral_sin_movimiento_h} h</small><strong>${r.sin_movimiento}</strong><span>necesitan seguimiento</span></div>
    <div class="kpi-libro k-ia"><small>En riesgo según la IA</small><strong>${r.en_riesgo}</strong><span>probabilidad &lt; 40 % o estancados</span></div>
    <div class="kpi-libro k-ok"><small>Mediana hasta aceptación</small><strong>${r.mediana_horas_aceptacion == null ? "—" : horasTxt(r.mediana_horas_aceptacion)}</strong><span>desde la radicación</span></div></div>`;

  // Tomos: un pestaña por mes de radicación (los más recientes primero)
  const meses = [...new Set(todos.map(a => String(a.radicada_en || a.fecha_solicitud).slice(0, 7)))].sort().reverse();
  const nombreMes = m => `${MESES[+m.slice(5, 7) - 1]} ${m.slice(0, 4)}`;
  $("#tomos").innerHTML = [`<button class="${estadoLibro.mes ? "" : "activa"}" data-mes="">Tomo completo</button>`,
    ...meses.slice(0, 5).map(m => `<button class="${estadoLibro.mes === m ? "activa" : ""}" data-mes="${m}">${esc(nombreMes(m))}</button>`)].join("");
  $$("#tomos button").forEach(b => b.addEventListener("click", () => { estadoLibro.mes = b.dataset.mes; estadoLibro.mostrar = 120; $$("#tomos button").forEach(x => x.classList.toggle("activa", x === b)); pintar(); }));

  const enRiesgo = a => a.abierta && (a.sin_movimiento || (a.probabilidad_efectiva != null && a.probabilidad_efectiva < 0.4));
  const vistas = { abiertas: a => a.abierta, cerradas: a => !a.abierta, riesgo: enRiesgo, todas: () => true };

  function pintar() {
    const t = estadoLibro.q.trim().toLowerCase();
    const delMes = todos.filter(a => !estadoLibro.mes || String(a.radicada_en || a.fecha_solicitud).startsWith(estadoLibro.mes));
    const n = k => delMes.filter(vistas[k]).length;
    $("#chips").innerHTML = [["abiertas", "Abiertos"], ["cerradas", "Cerrados"], ["riesgo", "En riesgo"], ["todas", "Todos"]]
      .map(([k, v]) => `<button class="chip ${k === "riesgo" ? "alerta" : ""} ${estadoLibro.vista === k ? "activa" : ""}" data-vista="${k}">${v} · ${n(k)}</button>`).join("");
    $$("#chips [data-vista]").forEach(b => b.addEventListener("click", () => { estadoLibro.vista = b.dataset.vista; estadoLibro.mostrar = 120; pintar(); }));

    const filas = delMes.filter(vistas[estadoLibro.vista]).filter(a => !t || [a.radicado, a.id, a.paciente_nombre, a.paciente_documento,
      a.institucion_origen, a.institucion_destino, a.eps].some(v => String(v ?? "").toLowerCase().includes(t)));
    const folios = [...new Set(filas.map(a => a.folio))].sort((x, y) => x - y);
    const ids = filas.map(a => a.id);
    $("#hoja-meta").innerHTML = `<div><small>TOMO</small><strong>${esc(filas[0] ? filas[0].tomo : "—")}</strong></div>
      <div><small>FOLIOS</small><strong>${folios.length ? `${String(folios[0]).padStart(4, "0")}${folios.length > 1 ? `–${String(folios[folios.length - 1]).padStart(4, "0")}` : ""}` : "—"}</strong></div>
      <div><small>ASIENTOS</small><strong>${ids.length ? `${String(Math.min(...ids)).padStart(6, "0")} – ${String(Math.max(...ids)).padStart(6, "0")}` : "—"}</strong></div>
      <div><small>EN ESTA VISTA</small><strong>${filas.length}</strong></div>`;

    if (!filas.length) { $("#l-tabla").innerHTML = `<div class="vacio">No hay asientos con esos filtros.</div>`; return; }
    const visibles = filas.slice(0, estadoLibro.mostrar);
    $("#l-tabla").innerHTML = `<table class="libro"><thead><tr><th>N.°</th><th>Fecha y hora</th><th>Paciente</th><th>Remite → Receptora</th>
      <th>Gestiones</th><th>Pronóstico IA</th><th>Cierre</th><th>Asentó</th></tr></thead><tbody>${visibles.map(a => {
        const [d, h] = partesFecha(a.radicada_en);
        const um = a.ultimo_movimiento;
        const cierre = a.abierta ? `${horasTxt(a.horas_abierta)} abierta` : partesFecha(a.cerrada_en).join(" · ");
        return `<tr class="clic ${enRiesgo(a) ? "riesgo" : ""} ${a.activo === false ? "inactiva" : ""}" data-id="${a.id}" tabindex="0">
          <td><span class="asiento-num">${String(a.id).padStart(6, "0")}</span><span class="sub-mono">${esc(a.radicado)}</span></td>
          <td class="col-fecha">${d}<span class="sub-mono">${h}</span></td>
          <td><div class="asiento-pac">${esc(a.paciente_nombre)}</div><span class="sub-mono">CC ${esc(a.paciente_documento)} · ${esc(a.eps)}</span></td>
          <td><span class="sub">${esc(a.institucion_origen)}</span><span class="ruta-flecha" aria-hidden="true">→ </span>${esc(a.institucion_acepta || a.institucion_destino)}</td>
          <td><div class="marcas">${a.marcas.map(m => marcaHTML(m)).join("") || `<span class="tenue">Sin contactos</span>`}</div>
            ${um ? `<span class="sub" title="${esc(um.respuesta)}">${esc(um.institucion || "")} · ${esc(hace(um.fecha))}</span>` : ""}
            ${a.sin_movimiento ? `<span class="sub" style="color:var(--error);font-weight:600">${horasTxt(a.horas_sin_movimiento)} sin movimiento</span>` : ""}</td>
          <td>${a.abierta ? barraIA(a.probabilidad_efectiva) : `<span class="tenue">—</span>`}</td>
          <td>${selloHTML(a.estado)}<span class="sub-mono" style="margin-top:.3rem">${esc(cierre)}</span></td>
          <td><span class="asento">${esc(a.asento_nombre || "—")}</span><span class="sub-mono">${esc(ROLES[a.asento_rol] || "")}</span></td></tr>`;
      }).join("")}</tbody></table>
      ${filas.length > visibles.length ? `<div class="vacio"><button class="btn btn-secundario btn-sm" id="l-mas">Mostrar ${Math.min(120, filas.length - visibles.length)} asientos más (${filas.length - visibles.length} restantes)</button></div>` : ""}`;
    $$("#l-tabla tr.clic").forEach(tr => {
      const abrir = () => ir("ficha", { remision: +tr.dataset.id });
      tr.addEventListener("click", abrir);
      tr.addEventListener("keydown", e => { if (e.key === "Enter") abrir(); });
    });
    const mas = $("#l-mas");
    if (mas) mas.addEventListener("click", () => { estadoLibro.mostrar += 120; pintar(); });
  }
  $("#l-q").addEventListener("input", debounce(e => { estadoLibro.q = e.target.value; estadoLibro.mostrar = 120; pintar(); }, 200));
  const inact = $("#l-inact");
  if (inact) inact.addEventListener("change", () => { estadoLibro.inactivas = inact.checked; vistaLibro(); });
  pintar();
}

// ==========================================================================
// FORMATO OFICIAL DE LA REMISIÓN
// ==========================================================================
const campoF = (k, v, extra = "") => `<div class="campo-f ${extra}"><small>${esc(k)}</small><span>${esc(v ?? "—")}</span></div>`;

async function vistaFormato({ remision }) {
  const main = $("#contenido");
  if (!remision) { ir("inicio"); return; }
  main.innerHTML = `<div class="pagina">${cargando("Abriendo el formato…")}</div>`;
  let f;
  try { f = await API.get(`/registro/remisiones/${remision}/formato`); }
  catch (e) { main.innerHTML = `<div class="pagina"><div class="tarjeta vacio">${esc(e.message)}</div></div>`; return; }
  const r = f.remision, p = f.paciente, per = f.permisos, ia = f.pronostico;
  const recargar = () => vistaFormato({ remision });
  const descargas = [
    { txt: "Constancia PDF", ruta: `/registro/remisiones/${r.id}/constancia.pdf`, nombre: `${r.radicado}_constancia.pdf` },
    { txt: "Bitácora Excel", ruta: `/registro/remisiones/${r.id}/bitacora.xlsx`, nombre: `${r.radicado}_bitacora.xlsx` },
    ...(per.anexos_clinicos ? [{ txt: "FHIR JSON", ruta: `/registro/remisiones/${r.id}/fhir.json`, nombre: `${r.radicado}_fhir.json` }] : []),
  ];
  const [rd, rh] = partesFecha(r.radicada_en);
  const vitalFuera = s => { const v = VITAL[s.codigo_loinc]; return v && (s.valor < v.min || s.valor > v.max); };
  const siguienteTxt = r.abierta ? `Siguiente gestión pendiente · ${horasTxt(r.horas_sin_movimiento)} desde el último registro` : "";

  main.innerHTML = `<div class="pagina">
    <div class="formato-barra" style="max-width:960px;margin:0 auto 1rem">
      <a href="#" class="volver" id="f-volver">‹ Libro de radicación · asiento ${String(r.id).padStart(6, "0")}</a>
      <div style="display:flex;gap:.5rem;flex-wrap:wrap;align-items:center">${barraDescargas(descargas)}
        <button class="btn btn-secundario btn-sm" id="f-imprimir">${icono("imprimir")}Imprimir</button>
        ${per.anotar ? `<button class="btn btn-coral" id="f-anotar">+ Anotar gestión</button>` : ""}</div>
    </div>
    <article class="formato" aria-label="Formato de registro ${esc(r.radicado)}">
      <div class="fmt-enc">
        <div class="logo">HUV</div>
        <div class="titulo"><small>Hospital Universitario del Valle «Evaristo García» E.S.E.</small>
          <strong>Registro de Referencia y Contrarreferencia de Pacientes</strong></div>
        <div class="codigos"><span>Código: TR-FR-01</span><span>Versión: 02</span><span>Folio ${String(r.folio).padStart(4, "0")} · Asiento ${String(r.id).padStart(6, "0")}</span></div>
      </div>
      <div class="fmt-franja">
        <div class="dato"><small>N.° de radicado</small><span class="grande">${esc(r.radicado)}</span></div>
        <div class="datos">
          <div class="dato"><small>Radicada</small><span class="mono">${rd} · ${rh}</span></div>
          <div class="dato"><small>Tiempo</small><span class="mono">${esc(f.tiempo_texto)}</span></div>
          <div class="dato"><small>Estado</small>${selloHTML(r.estado)}</div>
          ${r.activo === false ? `<div class="dato"><small>Registro</small><span style="color:var(--error);font-weight:700">ANULADO</span></div>` : ""}
        </div>
      </div>

      <section class="fmt-sec"><h3>1. Identificación del paciente</h3>
        <div class="campos-f c4">${campoF("Nombres y apellidos", p.nombre)}${campoF("Documento", `CC ${p.documento}`)}
          ${campoF("Edad / sexo", `${p.edad ?? "—"} años · ${p.genero || "—"}`)}${campoF("EPS", p.eps)}
          ${campoF("Teléfono", p.telefono)}${campoF("Tipo de sangre", p.tipo_sangre)}${campoF("Alergias", p.alergias)}${campoF("Paciente N.°", p.id)}</div></section>

      <section class="fmt-sec"><h3>2. Datos de la solicitud</h3>
        <div class="campos-f c2">${campoF("Institución que remite", r.institucion_origen)}${campoF("Institución receptora", r.institucion_destino)}
          ${campoF("Médico que radica", r.asento_nombre)}${campoF("Convenio con la receptora", r.convenio_vigente ? "Vigente" : "Sin convenio")}
          ${campoF("Diagnóstico", f.diagnostico)}${campoF("Motivo de remisión", f.requiere)}</div>
        ${f.signos.length ? `<div class="campos-f c4">${f.signos.map(s => campoF(s.tipo, `${Number(s.valor).toLocaleString("es-CO")} ${unidadBonita(s.unidad)}`, vitalFuera(s) ? "fuera" : "")).join("")}</div>` : ""}
      </section>

      <section class="fmt-sec"><h3>3. Bitácora de gestión · cada fila es un registro firmado, no editable</h3>
        <div class="tabla-f"><table><thead><tr><th>N.°</th><th>Fecha / hora</th><th>Institución · medio</th><th>Respuesta anotada</th><th>Causa (IA)</th><th>Registró</th></tr></thead>
          <tbody>${f.bitacora.map(b => `<tr><td class="mono">${String(b.orden).padStart(2, "0")}</td>
            <td class="mono" style="white-space:nowrap">${partesFecha(b.fecha).join(" ")}</td>
            <td>${esc(b.institucion)}<span class="sub">${esc(b.medio || "")}</span></td><td>${esc(b.respuesta)}</td>
            <td><span class="causa c-${esc(b.efecto)} ${esc(b.causa)}" title="Clasificó: ${esc(b.causa_origen)}">${esc(b.causa_etiqueta)}</span></td>
            <td style="white-space:nowrap">${esc(b.por || "—")}</td></tr>`).join("")}
            ${!f.bitacora.length ? `<tr><td colspan="6" class="tenue">Todavía no hay gestiones anotadas.</td></tr>` : ""}
            ${r.abierta ? `<tr class="siguiente"><td class="mono">${String(f.bitacora.length + 1).padStart(2, "0")}</td><td colspan="5">${esc(siguienteTxt)}</td></tr>` : ""}</tbody></table></div>
        ${per.anotar || per.cerrar || per.corregir || per.anular || per.restaurar ? `<div class="fmt-acciones">
          ${per.anotar ? `<button class="btn btn-coral btn-sm" id="f-anotar2">${icono("telefono")}Anotar gestión</button>` : ""}
          ${per.cerrar ? `<button class="btn btn-primario btn-sm" id="f-cerrar">${icono("cerrar")}Cerrar asiento</button>` : ""}
          ${per.corregir ? `<button class="btn btn-secundario btn-sm" id="f-corregir">Anotar corrección</button>` : ""}
          ${per.anular ? `<button class="btn btn-peligro btn-sm" id="f-anular">Anular asiento</button>` : ""}
          ${per.restaurar ? `<button class="btn btn-ok btn-sm" id="f-restaurar">Restaurar asiento</button>` : ""}
          </div>` : ""}
      </section>

      ${ia || f.resultado_ia ? `<section class="fmt-sec"><h3 class="ia">4. Apoyo de IA · orientativo, no reemplaza el criterio del equipo
          ${r.abierta && per.anexos_clinicos ? `<button class="btn btn-sm btn-suave" id="f-recalcular">Recalcular</button>` : ""}</h3>
        <div class="ia-grid">
          <div><small>Probabilidad de que termine efectiva</small>
            <div style="display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap"><span class="grande ${claseIA(ia ? ia.probabilidad_efectiva : 0)}">${ia ? Math.round(100 * ia.probabilidad_efectiva) : "—"}%</span>
              ${ia && ia.probabilidad_al_radicar != null ? `<span class="tenue">al radicar: ${Math.round(100 * ia.probabilidad_al_radicar)}%</span>` : ""}</div>
            ${ia ? `<span class="pista"><span class="${claseIA(ia.probabilidad_efectiva)}-bg" style="width:${Math.round(100 * ia.probabilidad_efectiva)}%"></span></span>` : ""}
            <span class="tenue">${f.resultado_ia ? (f.resultado_ia.acerto ? "Al cierre: la IA acertó." : "Al cierre: la IA no acertó.") : "Se recalcula con cada gestión anotada."}</span></div>
          <div><small>Tiempo estimado hasta aceptación</small>
            <span class="grande" style="color:var(--azul-oscuro)">${ia && ia.tiempo_estimado_horas ? (ia.tiempo_estimado_horas.p75 < 1 ? "< 1 h" : `${horasTxt(ia.tiempo_estimado_horas.mediana)}–${horasTxt(ia.tiempo_estimado_horas.p75)}`) : "—"}</span>
            <span class="tenue">${ia && ia.tiempo_estimado_horas ? `Según ${ia.tiempo_estimado_horas.n_similares} remisiones efectivas parecidas (mismo tipo de motivo).` : ""}</span>
            ${ia && ia.alerta_sin_movimiento ? `<span style="color:var(--error);font-weight:600">Lleva más tiempo de lo normal sin resolverse.</span>` : ""}</div>
          <div><small>${r.abierta ? "Si no aceptan, contactar" : "Instituciones sugeridas"}</small>
            ${ia && ia.instituciones_sugeridas && ia.instituciones_sugeridas.length ? ia.instituciones_sugeridas.map((s, i) => `<div class="sugerida"><span><span class="mono tenue">${i + 1}.</span> ${esc(s.institucion)}</span><span>${Math.round(100 * s.tasa_aceptacion)}%</span></div>`).join("")
              + `<span class="tenue">% de aceptación histórica (suavizada) para ${esc(p.eps)} y este tipo de caso.</span>`
              : `<span class="tenue">${!r.abierta ? "Asiento cerrado." : "El paciente llega al HUV: no aplica sugerir otra institución."}</span>`}</div>
        </div></section>` : ""}

      ${per.costos && f.costos ? `<section class="fmt-sec"><h3>5. Costos asociados al radicado</h3>
        <div class="tabla-f"><table style="min-width:560px"><thead><tr><th>Concepto</th><th>Cantidad</th><th style="text-align:right">Valor</th><th>Cobro a</th></tr></thead>
          <tbody>${f.costos.conceptos.map(c => `<tr><td>${esc(c.concepto)}</td><td class="mono">${esc(c.cantidad)} ${esc(c.unidad)}</td><td class="mono" style="text-align:right">${pesos(c.valor)}</td><td>${esc(f.costos.cobro_a)}</td></tr>`).join("")}
            <tr class="total"><td colspan="2">${f.costos.abierta ? "Acumulado a la fecha" : "Total"}</td><td class="mono" style="text-align:right">${pesos(f.costos.total)}</td>
              <td><span class="cobro ${esc(f.costos.estado_cobro)}">${esc(f.costos.estado_cobro_texto)}</span>${f.costos.factura ? ` <span class="mono tenue">${esc(f.costos.factura.numero || "")}</span>` : ""}</td></tr></tbody></table></div></section>` : ""}

      <section class="fmt-sec"><h3>6. Cierre de la remisión</h3>
        <div class="campos-f c3">${campoF("Institución que acepta", f.cierre.institucion_acepta)}${campoF("Fecha / hora de cierre", f.cierre.cerrada_en ? partesFecha(f.cierre.cerrada_en).join(" · ") : null)}
          ${campoF("Resultado", f.cierre.resultado_texto || "— (efectiva / fallida)")}</div></section>

      <section class="fmt-sec"><h3>7. Recorrido completo del asiento <span style="font-weight:500;text-transform:none;letter-spacing:0">${f.eventos.length} eventos</span></h3>
        <ol class="recorrido">${f.eventos.map(e => `<li class="t-${esc(e.tipo)} ${esc(e.efecto || "")}">
          <span class="cuando">${partesFecha(e.fecha).join(" ")}</span><span class="eje"><i></i><b></b></span>
          <div class="que"><strong>${esc(e.titulo)}</strong>${e.detalle ? `<p>${esc(e.detalle)}</p>` : ""}${e.por ? `<small>${esc(e.por)}</small>` : ""}</div></li>`).join("")}</ol></section>

      <div class="firmas"><div><strong>${esc(r.asento_nombre || "—")}</strong><small>Médico que radica · ${rd} ${rh}</small></div>
        <div><strong>${esc(f.responsable || "—")}</strong><small>Oficina de Referencia · responsable de la gestión</small></div></div>
      <p class="fmt-nota">Documento generado por TrazaRed HUV a partir de los registros del sistema (PostgreSQL, MongoDB y HAPI FHIR). La bitácora no puede modificarse; las correcciones se anotan como nuevas entradas con referencia a la original.</p>
    </article>
    ${per.anexos_clinicos ? `<details class="anexos" id="anexos"><summary>Anexos clínicos: datos del paciente, signos vitales e imágenes (PACS)</summary>
      <section class="tarjeta"><nav class="pestanas" role="tablist" style="padding:.8rem 1rem 0">
        <button role="tab" data-anexo="paciente" class="activa">Datos y documentos</button><button role="tab" data-anexo="clinica">Signos vitales</button><button role="tab" data-anexo="imagenes">Imágenes</button></nav>
        <div class="ficha-cuerpo" style="display:block"><div id="anexo-contenido"></div></div></section></details>` : ""}
  </div>`;

  activarDescargas(main, descargas);
  const on = (sel, fn) => { const b = $(sel, main); if (b) b.addEventListener("click", fn); };
  on("#f-volver", (e) => { e.preventDefault(); ir("inicio"); });
  on("#f-imprimir", () => window.print());
  on("#f-anotar", () => formularioGestion(f, recargar));
  on("#f-anotar2", () => formularioGestion(f, recargar));
  on("#f-cerrar", () => formularioCierre(f, recargar));
  on("#f-corregir", () => formularioRemision({ ...r }));
  on("#f-anular", async () => {
    if (!confirm(`¿Anular el asiento ${r.radicado}? No se borra: queda marcado como anulado en el libro y en la auditoría, y un administrador puede restaurarlo.`)) return;
    try { await API.del(`/remisiones/${r.id}`); remisionesCache = null; toast(`${r.radicado} anulado`, "ok"); ir("inicio"); } catch (e) { toast(e.message, "error"); }
  });
  on("#f-restaurar", async () => {
    try { await API.post(`/remisiones/${r.id}/restaurar`); remisionesCache = null; toast(`${r.radicado} restaurado`, "ok"); recargar(); } catch (e) { toast(e.message, "error"); }
  });
  on("#f-recalcular", async (e) => {
    e.target.disabled = true;
    try { await API.post(`/registro/remisiones/${r.id}/pronostico`); toast("Pronóstico recalculado y anotado en el recorrido", "ok"); recargar(); }
    catch (x) { toast(x.message, "error"); e.target.disabled = false; }
  });

  // Anexos clínicos: reutilizan las pestañas de la ficha anterior
  const anexos = $("#anexos");
  if (anexos) {
    let obs = null;
    const pac = { ...p };
    const rem = { ...r };
    const mostrar = async (tab) => {
      $$("[data-anexo]", anexos).forEach(b => b.classList.toggle("activa", b.dataset.anexo === tab));
      liberarImagenes();
      const cont = $("#anexo-contenido");
      if (tab === "clinica" && obs === null) {
        cont.innerHTML = cargando();
        try { obs = await API.get(`/observaciones?remision_id=${r.id}`); } catch (e) { cont.innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
      }
      const contexto = { r: rem, p: pac, obs: obs || [], recargar };
      ({ paciente: tabPaciente, clinica: tabClinica, imagenes: tabImagenes })[tab](cont, contexto);
    };
    $$("[data-anexo]", anexos).forEach(b => b.addEventListener("click", () => mostrar(b.dataset.anexo)));
    anexos.addEventListener("toggle", () => { if (anexos.open && !$("#anexo-contenido").innerHTML) mostrar("paciente"); });
  }
}

function formularioGestion(f, recargar) {
  const r = f.remision;
  const sugeridas = (f.pronostico && f.pronostico.instituciones_sugeridas || []).map(s => s.institucion);
  const lista = [...new Set([...sugeridas, ...INSTITUCIONES])];
  abrirFormulario({
    titulo: `Anotar gestión · ${r.radicado}`, textoBoton: "Anotar en la bitácora",
    campos: `<p class="tenue" style="margin:-.2rem 0 .9rem">Lo que se anota queda firmado con tu usuario y la hora, y no se puede editar ni borrar.</p>
      <div class="campo"><label for="g-ins">Institución contactada</label><input id="g-ins" name="institucion_contactada" list="dl-g-ins" required
        value="${esc(sugeridas[0] || r.institucion_destino || "")}" placeholder="Escribe o elige">${datalist("dl-g-ins", lista)}</div>
      ${sugeridas.length ? `<p class="tenue" style="margin:-.5rem 0 .9rem">Sugeridas por la IA: ${sugeridas.map(esc).join(" · ")}</p>` : ""}
      <div class="fila"><div class="campo"><label for="g-med">Medio</label><select id="g-med" name="medio"><option value="telefono">Teléfono</option>
          <option value="plataforma">Plataforma de referencia</option><option value="correo">Correo</option><option value="presencial">Presencial</option></select></div>
        <div class="campo"><label for="g-por">Contactado por</label><input id="g-por" name="contactado_por" required value="${esc(sesion.usuario.nombre)}"></div></div>
      <div class="campo"><label for="g-res">Respuesta de la institución</label><input id="g-res" name="respuesta" required maxlength="500" placeholder="Ej.: Sin cupo en UCI / Aceptan, enviar en ambulancia"></div>
      <p class="prevista-causa" id="g-prev" aria-live="polite"></p>`,
    alAbrir(form) {
      const res = $("#g-res", form), prev = $("#g-prev", form);
      res.addEventListener("input", debounce(async () => {
        const t = res.value.trim();
        if (t.length < 3) { prev.textContent = ""; return; }
        try {
          const k = await API.post("/registro/ia/clasificar", { texto: t });
          prev.innerHTML = `La IA lo anotará como <span class="causa c-${esc(k.efecto)} ${esc(k.causa)}">${esc(k.etiqueta)}</span>${k.efecto === "acepta" ? " · el asiento pasará a <strong>Aceptada</strong>" : ""}`;
        } catch { prev.textContent = ""; }
      }, 350));
      $("#g-res", form).focus();
    },
    async alGuardar(d) {
      if (!d.institucion_contactada.trim() || !d.respuesta.trim()) throw new Error("Completa la institución y la respuesta.");
      const g = await API.post(`/registro/remisiones/${r.id}/gestiones`, {
        institucion_contactada: d.institucion_contactada.trim(), medio: d.medio,
        respuesta: d.respuesta.trim(), contactado_por: d.contactado_por.trim() });
      remisionesCache = null;
      toast(`Gestión anotada · ${g.clasificacion.etiqueta}${g.estado_cambio ? ` · el asiento pasó a ${ESTADOS[g.estado]}` : ""}`, "ok");
      recargar();
    },
  });
}

function formularioCierre(f, recargar) {
  const r = f.remision;
  abrirFormulario({
    titulo: `Cerrar asiento · ${r.radicado}`, textoBoton: "Cerrar asiento",
    campos: `<p class="tenue" style="margin:-.2rem 0 .9rem">El cierre es definitivo: deja el asiento listo para facturar en el libro contable y evalúa si la IA acertó.</p>
      <div class="campo"><label for="c-res">Resultado</label><select id="c-res" name="resultado">
        <option value="trasladado" ${r.estado === "aceptada" ? "selected" : ""}>Efectiva · el paciente fue trasladado</option>
        <option value="rechazada" ${r.estado !== "aceptada" ? "selected" : ""}>Fallida · no se logró el traslado</option></select></div>
      <div class="campo" id="c-campo-inst"><label for="c-ins">Institución que recibió al paciente</label>
        <input id="c-ins" name="institucion_acepta" list="dl-c-ins" value="${esc(r.institucion_acepta || "")}">${datalist("dl-c-ins", INSTITUCIONES)}</div>
      <div class="campo"><label for="c-nota">Nota de cierre (opcional)</label><input id="c-nota" name="nota" maxlength="300" placeholder="Ej.: traslado en ambulancia medicalizada TAM-12"></div>`,
    alAbrir(form) {
      const s = $("#c-res", form);
      const f2 = () => $("#c-campo-inst", form).classList.toggle("oculto", s.value !== "trasladado");
      s.addEventListener("change", f2); f2();
    },
    async alGuardar(d) {
      if (d.resultado === "trasladado" && !String(d.institucion_acepta || "").trim()) throw new Error("Indica la institución que recibió al paciente.");
      const c = await API.post(`/registro/remisiones/${r.id}/cierre`, { resultado: d.resultado,
        institucion_acepta: String(d.institucion_acepta || "").trim() || null, nota: String(d.nota || "").trim() || null });
      remisionesCache = null;
      toast(`${c.radicado} cerrado · ${ESTADOS[c.estado]}${c.ia_acerto == null ? "" : c.ia_acerto ? " · la IA acertó" : " · la IA no acertó"}`, "ok");
      recargar();
    },
  });
}

// ==========================================================================
// SEGUIMIENTO DEL PACIENTE (rastreo, sin datos del personal)
// ==========================================================================
async function vistaSeguimiento() {
  const u = sesion.usuario;
  $("#contenido").innerHTML = `<div class="pagina">${cargando("Buscando tu remisión…")}</div>`;
  let lista;
  try { lista = await API.get("/registro/mi-seguimiento"); }
  catch (e) { $("#contenido").innerHTML = `<div class="pagina"><div class="tarjeta vacio">${esc(e.message)}</div></div>`; return; }
  lista.sort((a, b) => String(b.radicada_en).localeCompare(String(a.radicada_en)));
  const r = lista[0];
  if (!r) { $("#contenido").innerHTML = `<div class="pagina"><div class="seguimiento"><div class="seg-cabeza"><h2>Hola, ${esc(primerNombre(u.nombre))}</h2><p>No tienes remisiones registradas.</p></div></div></div>`; return; }
  const fallida = r.estado === "rechazada";
  const pasos = [
    ["Remisión registrada", `${partesFecha(r.radicada_en).join(" · ")} · radicado ${r.radicado}`],
    [fallida ? "No se logró el traslado" : "Buscando hospital", fallida ? "Las instituciones contactadas no pudieron recibirte. El hospital te contará los siguientes pasos." : `Se ha contactado a ${r.instituciones_contactadas} institución${r.instituciones_contactadas === 1 ? "" : "es"}`],
    ["Hospital confirmado", r.institucion_acepta ? `${r.institucion_acepta} aceptó recibirte` : "Te avisaremos apenas acepten"],
    ["Traslado", r.estado === "completada" ? `Realizado · ${partesFecha(r.cerrada_en).join(" · ")}` : "Se asigna ambulancia y hora de salida"],
  ];
  const clase = i => (fallida && i === 1) ? "fallo" : (i + 1 < r.paso || r.estado === "completada" || (i + 1 === r.paso && r.estado === "aceptada" && i === 2)) ? "hecho" : (i + 1 === r.paso ? "actual" : "");
  $("#contenido").innerHTML = `<div class="pagina"><div class="seguimiento">
    <div class="seg-cabeza"><small style="color:#b9dcd8">TrazaRed HUV</small><h2>Hola, ${esc(primerNombre(u.nombre))}</h2><p>Así va tu remisión, en tiempo real.</p></div>
    <section class="seg-tarjeta flota">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:.5rem"><span class="radicado" style="font-weight:600;color:var(--navy)">${esc(r.radicado)}</span>${pildora(r.estado)}</div>
      <div class="seg-tiempo"><small class="tenue">${r.estado === "completada" || fallida ? "Duración de la gestión" : "Estamos buscando hospital desde hace"}</small><strong>${esc(r.tiempo_texto)}</strong></div>
      <ol class="seg-pasos">${pasos.map(([t, d], i) => `<li class="${clase(i)}"><span class="p-eje"><i></i><b></b></span><div><strong>${esc(t)}</strong><small>${esc(d)}</small></div></li>`).join("")}</ol>
    </section>
    ${r.ultimo_movimiento ? `<section class="seg-tarjeta"><small class="tenue" style="text-transform:uppercase;letter-spacing:.05em;font-weight:600">Último movimiento · ${partesFecha(r.ultimo_movimiento.fecha).join(" ")}</small>
      <p>${esc(r.ultimo_movimiento.texto)}</p></section>` : ""}
    <section class="seg-tarjeta"><small class="tenue" style="text-transform:uppercase;letter-spacing:.05em;font-weight:600">¿Qué significa?</small><p>${esc(EXPLICA_PACIENTE[r.estado] || "")}</p>
      <p class="tenue">Si tienes preguntas, la Oficina de Referencia y Contrarreferencia del HUV puede contarte más sobre tu traslado.</p></section>
    ${lista.length > 1 ? `<section class="seg-tarjeta"><small class="tenue" style="text-transform:uppercase;letter-spacing:.05em;font-weight:600">Tus otras remisiones</small>
      ${lista.slice(1).map(x => `<div style="display:flex;justify-content:space-between;gap:.5rem"><span class="radicado">${esc(x.radicado)}</span>${pildora(x.estado)}</div>`).join("")}</section>` : ""}
  </div></div>`;
}

// ==========================================================================
// LIBRO CONTABLE POR RADICADO
// ==========================================================================
const estadoContable = { cobro: "", q: "", eps: "" };
const CONCEPTO_CORTO = {
  "EST-URG": c => `Urgencias ${String(c.cantidad).replace(".", ",")} h`, "EST-UCI": c => `UCI ${String(c.cantidad).replace(".", ",")} h`,
  "GES-REF": c => `${c.cantidad} gestiones`, "TRA-MED": () => "Ambulancia medicalizada", "TRA-BAS": () => "Ambulancia básica",
};
const RESULTADO_C = { pendiente: "EN CURSO", en_gestion: "EN CURSO", aceptada: "ACEPTADA", rechazada: "RECHAZADA", completada: "TRASLADADO" };

async function vistaLibroContable() {
  const u = sesion.usuario;
  if (!["admin", "contable"].includes(u.rol)) { $("#contenido").innerHTML = `<div class="pagina"><div class="tarjeta vacio">Solo el rol contable o admin pueden ver el libro contable.</div></div>`; return; }
  const descargas = [
    { txt: "Excel", ruta: "/contabilidad/libro.xlsx", nombre: `libro_contable_${hoyArchivo()}.xlsx` },
    { txt: "PDF", ruta: "/contabilidad/libro.pdf", nombre: `libro_contable_${hoyArchivo()}.pdf` },
    { txt: "RIPS (JSON)", ruta: "/contabilidad/rips.json", nombre: `rips_${hoyArchivo()}.json` },
  ];
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><span class="mono tenue">Valores de referencia del proyecto · no es el tarifario oficial del HUV</span>
      <h2 class="serif" style="font-size:1.8rem">Libro contable por radicado</h2>
      <p>Lo que costó cada remisión y a quién se le cobra. Se llena solo desde la bitácora; aquí solo se factura, se registran glosas y pagos.</p></div>
      ${barraDescargas(descargas)}</div>
    <div id="c-kpis">${cargando()}</div>
    <div class="contable-grid">
      <section><div class="tabla-contable">
        <div class="filtros"><div class="buscar">${icono("buscar")}<input type="search" id="c-q" aria-label="Buscar" placeholder="Radicado, paciente, documento o factura…" value="${esc(estadoContable.q)}"></div>
          <select id="c-eps" aria-label="EPS"><option value="">Todas las EPS</option>${EPS.map(e => `<option ${estadoContable.eps === e ? "selected" : ""}>${esc(e)}</option>`).join("")}</select>
          <div id="c-chips" style="display:flex;gap:.4rem;flex-wrap:wrap"></div><span class="total" id="c-total"></span></div>
        <div class="tabla-scroll" id="c-tabla">${cargando("Liquidando remisiones…")}</div></div></section>
      <aside id="c-lado">${cargando()}</aside>
    </div>
    <details class="tarjeta" style="margin-top:1rem;padding:0 1.2rem" id="c-sueltas"><summary style="cursor:pointer;padding:1rem 0;font-weight:700;color:var(--azul-oscuro)">Facturas de otros servicios (análisis de IA, consultas)</summary><div id="c-sueltas-cuerpo" style="padding-bottom:1rem"></div></details>
  </div>`;
  activarDescargas($("#contenido"), descargas);

  let filas, resumen;
  async function cargar() {
    try { [filas, resumen] = await Promise.all([API.get("/contabilidad/libro"), API.get("/contabilidad/resumen")]); }
    catch (e) { $("#c-tabla").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
    const k = (cls, t, b, sub) => `<div class="kpi-libro ${cls}"><small>${t}</small><strong>${pesosCortos(b.valor)}</strong><span>${b.n} ${sub}</span></div>`;
    $("#c-kpis").innerHTML = `<div class="kpis-libro">${k("k-ia", "Acumulando (abiertas)", resumen.acumulando, "radicados en curso")}
      ${k("k-alerta", "Por facturar", resumen.por_facturar, "radicados cerrados")}${k("", "Facturado a EPS", resumen.facturado, "facturas pendientes de pago")}
      ${k("k-coral", "Glosado", resumen.glosado, "facturas objetadas")}${k("k-ok", "Pagado", resumen.pagado, "facturas")}
      ${k("k-tinta", "Costo de remisiones fallidas", resumen.costo_fallidas, "rechazadas · lo que no se recupera")}</div>`;
    const max = Math.max(1, ...resumen.por_eps.map(e => e.horas_espera));
    $("#c-lado").innerHTML = `<h3>Costo de la espera por EPS</h3>
      <p class="tenue">Horas de urgencias o UCI ocupadas por pacientes esperando traslado (tope ${resumen.tope_horas} h por remisión).</p>
      ${resumen.por_eps.slice(0, 8).map(e => `<div class="barra-eps"><div><span>${esc(e.eps)}</span><span class="mono" style="font-weight:600">${Math.round(e.horas_espera).toLocaleString("es-CO")} h</span></div>
        <span class="pista"><span style="width:${(100 * e.horas_espera) / max}%"></span></span><small class="tenue">${e.remisiones} remisiones · ${e.rechazadas} rechazadas · ${pesosCortos(e.costo)}</small></div>`).join("")}
      <h3 style="margin-top:.6rem">Tarifas de referencia</h3>
      <dl class="datos" style="font-size:.82rem">${Object.entries(resumen.tarifas).map(([k2, v]) => `<dt>${esc(k2.replace(/_/g, " "))}</dt><dd class="mono">${pesos(v)}</dd>`).join("")}</dl>`;
    pintar();
  }
  const COBROS = [["", "Todos"], ["acumulando", "Acumulando"], ["por_facturar", "Por facturar"], ["facturada", "Facturadas"], ["glosada", "Glosadas"], ["pagada", "Pagadas"]];
  function pintar() {
    const t = estadoContable.q.trim().toLowerCase();
    const base = filas.filter(f => (!estadoContable.eps || f.eps === estadoContable.eps) && (!t || [f.radicado, f.paciente_nombre, f.paciente_documento, f.factura && f.factura.numero]
      .some(v => String(v ?? "").toLowerCase().includes(t))));
    $("#c-chips").innerHTML = COBROS.map(([k2, v]) => `<button class="chip ${estadoContable.cobro === k2 ? "activa" : ""}" data-cobro="${k2}">${v} · ${base.filter(f => !k2 || f.estado_cobro === k2).length}</button>`).join("");
    $$("#c-chips [data-cobro]").forEach(b => b.addEventListener("click", () => { estadoContable.cobro = b.dataset.cobro; pintar(); }));
    const lista = base.filter(f => !estadoContable.cobro || f.estado_cobro === estadoContable.cobro)
      .sort((a, b) => ({ por_facturar: 0, glosada: 1, facturada: 2, acumulando: 3, pagada: 4 }[a.estado_cobro] - { por_facturar: 0, glosada: 1, facturada: 2, acumulando: 3, pagada: 4 }[b.estado_cobro]) || b.remision_id - a.remision_id);
    $("#c-total").textContent = `${lista.length} radicados · ${pesosCortos(lista.reduce((s, f) => s + f.total, 0))}`;
    if (!lista.length) { $("#c-tabla").innerHTML = `<div class="vacio">No hay radicados con esos filtros.</div>`; return; }
    $("#c-tabla").innerHTML = `<table><thead><tr><th>Asiento</th><th>Paciente · EPS</th><th>Resultado</th><th>Conceptos</th><th style="text-align:right">Costo</th><th>Cobro</th><th></th></tr></thead>
      <tbody>${lista.slice(0, 300).map(f => `<tr>
        <td>${u.rol === "admin" ? `<a href="#" class="asiento-num" data-ver="${f.remision_id}">${esc(f.radicado)}</a>` : `<span class="asiento-num">${esc(f.radicado)}</span>`}<span class="sub-mono">${partesFecha(f.radicada_en).join(" ")}</span></td>
        <td>${esc(f.paciente_nombre)}<span class="sub">CC ${esc(f.paciente_documento)} · ${esc(f.eps)}</span></td>
        <td>${selloHTML(f.estado)}</td>
        <td style="font-size:.78rem;color:var(--texto-suave)">${f.conceptos.map(c => esc(CONCEPTO_CORTO[c.codigo] ? CONCEPTO_CORTO[c.codigo](c) : c.concepto)).join(" · ")}</td>
        <td class="mono" style="text-align:right;font-weight:600">${pesos(f.total)}</td>
        <td><span class="cobro ${esc(f.estado_cobro)}">${esc(f.estado_cobro_texto)}</span>${f.factura ? `<span class="sub-mono">${esc(f.factura.numero || "")}${f.factura.motivo_glosada && f.estado_cobro === "glosada" ? ` · ${esc(f.factura.motivo_glosada)}` : ""}</span>` : ""}</td>
        <td style="white-space:nowrap">${f.estado_cobro === "por_facturar" ? `<button class="btn btn-primario btn-sm" data-facturar="${f.remision_id}">Facturar</button>` : ""}
          ${f.estado_cobro === "facturada" ? `<button class="btn btn-ok btn-sm" data-pagar="${f.factura._id}">Pagada</button> <button class="btn btn-secundario btn-sm" data-glosar="${f.factura._id}">Glosa</button>` : ""}
          ${f.estado_cobro === "glosada" ? `<button class="btn btn-ok btn-sm" data-pagar="${f.factura._id}">Pagada</button> <button class="btn btn-peligro btn-sm" data-anular="${f.factura._id}">Anular</button>` : ""}</td></tr>`).join("")}</tbody></table>
      ${lista.length > 300 ? `<div class="vacio">Se muestran 300 de ${lista.length}. Usa los filtros o descarga el Excel completo.</div>` : ""}`;
    const accion = (sel, fn) => $$(sel, $("#c-tabla")).forEach(b => b.addEventListener("click", async (e) => {
      e.preventDefault(); b.disabled = true;
      try { await fn(b); await cargar(); } catch (x) { toast(x.message, "error"); b.disabled = false; }
    }));
    accion("[data-facturar]", async b => { const fac = await API.post(`/contabilidad/remisiones/${b.dataset.facturar}/facturar`); toast(`Factura ${fac.numero} emitida a ${fac.eps} por ${pesos(fac.valor)}`, "ok"); });
    accion("[data-pagar]", async b => { await API.put(`/facturas/${b.dataset.pagar}/pagar`); toast("Pago registrado", "ok"); });
    accion("[data-glosar]", async b => { const m = prompt("Motivo de la glosa de la EPS:"); if (!m || m.trim().length < 3) throw new Error("Escribe el motivo de la glosa."); await API.put(`/facturas/${b.dataset.glosar}/glosar`, { motivo: m.trim() }); toast("Glosa registrada", "ok"); });
    accion("[data-anular]", async b => { const m = prompt("Motivo de la anulación (la remisión vuelve a quedar por facturar):"); if (!m || m.trim().length < 3) throw new Error("Escribe el motivo de la anulación."); await API.put(`/facturas/${b.dataset.anular}/anular`, { motivo: m.trim() }); toast("Factura anulada", "ok"); });
    $$("[data-ver]", $("#c-tabla")).forEach(a => a.addEventListener("click", (e) => { e.preventDefault(); ir("ficha", { remision: +a.dataset.ver, desde: "contabilidad" }); }));
  }
  $("#c-q").addEventListener("input", debounce(e => { estadoContable.q = e.target.value; if (filas) pintar(); }, 200));
  $("#c-eps").addEventListener("change", e => { estadoContable.eps = e.target.value; if (filas) pintar(); });
  $("#c-sueltas").addEventListener("toggle", async () => {
    const cuerpo = $("#c-sueltas-cuerpo");
    if (!$("#c-sueltas").open || cuerpo.innerHTML) return;
    cuerpo.innerHTML = cargando();
    try {
      const fs = (await API.get("/facturas")).filter(f => f.servicio !== "remision");
      cuerpo.innerHTML = fs.length ? `<table><thead><tr><th>Fecha</th><th>Factura</th><th>Paciente</th><th>Servicio</th><th>Valor</th><th>Estado</th><th></th></tr></thead><tbody>${fs.map(f => `<tr>
        <td class="num">${fechaHora(f.fecha)}</td><td class="mono">${esc(f.numero || "—")}</td><td>Paciente #${esc(f.paciente_id)}</td><td>${esc(f.servicio)}${f.reporte_id ? ` · reporte #${esc(f.reporte_id)}` : ""}</td>
        <td class="mono">${pesos(f.valor)}</td><td><span class="cobro ${f.estado_pago === "pendiente" ? "facturada" : esc(f.estado_pago)}">${esc(f.estado_pago)}</span></td>
        <td>${f.estado_pago === "pendiente" ? `<button class="btn btn-ok btn-sm" data-pagar-suelta="${f._id}">Pagada</button>` : ""}</td></tr>`).join("")}</tbody></table>` : `<p class="tenue">No hay facturas de otros servicios.</p>`;
      $$("[data-pagar-suelta]", cuerpo).forEach(b => b.addEventListener("click", async () => {
        try { await API.put(`/facturas/${b.dataset.pagarSuelta}/pagar`); toast("Pago registrado", "ok"); cuerpo.innerHTML = ""; $("#c-sueltas").open = false; } catch (x) { toast(x.message, "error"); }
      }));
    } catch (x) { cuerpo.innerHTML = `<div class="vacio">${esc(x.message)}</div>`; }
  });
  cargar();
}

// ==========================================================================
// PRONÓSTICOS DE REMISIONES (IA del registro)
// ==========================================================================
async function vistaPronosticos() {
  const u = sesion.usuario;
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Pronósticos de remisiones</h2>
      <p>La IA del registro: probabilidad de que cada remisión termine efectiva, institución sugerida y causa de cada respuesta anotada.</p></div>
      ${u.rol === "admin" ? `<div style="display:flex;gap:.5rem;flex-wrap:wrap"><button class="btn btn-secundario" id="p-recalc">Recalcular abiertas</button><button class="btn btn-primario" id="p-entrenar">Reentrenar modelo</button></div>` : ""}</div>
    <div id="p-cuerpo">${cargando("Consultando el modelo…")}</div></div>`;
  const on = (sel, fn) => { const b = $(sel); if (b) b.addEventListener("click", async () => { b.disabled = true; try { await fn(); vistaPronosticos(); } catch (e) { toast(e.message, "error"); b.disabled = false; } }); };
  on("#p-recalc", async () => { const r = await API.post("/registro/ia/recalcular"); toast(`${r.recalculadas} remisiones abiertas pronosticadas`, "ok"); });
  on("#p-entrenar", async () => { toast("Reentrenando… puede tardar unos segundos", "info"); const r = await API.post("/registro/ia/entrenar"); toast(`Modelo ${r.version} entrenado`, "ok"); });
  let d;
  try { d = await API.get("/registro/ia/resumen"); } catch (e) { $("#p-cuerpo").innerHTML = `<div class="tarjeta vacio">${esc(e.message)}</div>`; return; }
  const m = d.modelo, L = d.en_libro;
  const imp = lista => `<div class="importancias">${(lista || []).map(x => `<div><span>${esc(x.variable.replace(/_/g, " "))}</span><span class="pista"><span style="width:${Math.round(100 * x.importancia / Math.max(...lista.map(y => y.importancia)))}%"></span></span><b>${x.importancia.toFixed(2)}</b></div>`).join("")}</div>`;
  $("#p-cuerpo").innerHTML = `
    <div class="kpis-libro">
      <div class="kpi-libro k-ia"><small>Versión del modelo</small><strong style="font-size:1.1rem">${esc(m ? m.version : "sin servicio")}</strong><span>${m ? `entrenado ${fechaHora(m.entrenado_en)}` : "el servicio ml no respondió"}</span></div>
      <div class="kpi-libro"><small>AUC al radicar (validación cruzada)</small><strong>${m ? m.metricas.radicacion.auc.toFixed(2) : "—"}</strong><span>${m ? `${m.metricas.radicacion.n} remisiones cerradas` : ""}</span></div>
      <div class="kpi-libro"><small>AUC durante la gestión (por remisión)</small><strong>${m ? m.metricas.gestion.auc.toFixed(2) : "—"}</strong><span>${m ? `${m.metricas.gestion.n} fotos de la bitácora` : ""}</span></div>
      <div class="kpi-libro k-ok"><small>Acierto en este libro</small><strong>${L.tasa_acierto == null ? "—" : `${Math.round(100 * L.tasa_acierto)}%`}</strong><span>${L.cerradas_evaluadas} asientos cerrados con pronóstico</span></div>
      <div class="kpi-libro k-coral"><small>Abiertas en riesgo</small><strong>${L.en_riesgo}</strong><span>de ${L.abiertas} con pronóstico (&lt; 40 %)</span></div></div>
    <p class="aviso-honesto" style="margin-bottom:1rem"><strong>Para leer estas métricas:</strong> el modelo se entrena con el dataset sintético del proyecto (200 efectivas, 200 fallidas, 200 abiertas) y los grupos se generaron con reglas muy marcadas, por eso el AUC sale casi perfecto. Con datos reales del HUV las métricas serían más bajas. El estado de la remisión nunca entra al modelo, y el número de contactos y las horas de gestión solo entran contados hasta el momento del pronóstico.</p>
    <div class="graficas">
      <section class="tarjeta tarjeta-cuerpo"><h4>Qué pesa más al radicar</h4><p class="tenue" style="margin-bottom:.6rem">Importancia de cada variable en el modelo de radicación (XGBoost)</p>${m ? imp(m.importancias.radicacion) : ""}</section>
      <section class="tarjeta tarjeta-cuerpo"><h4>Qué pesa más durante la gestión</h4><p class="tenue" style="margin-bottom:.6rem">Incluye lo que va dejando la bitácora hasta ese momento</p>${m ? imp(m.importancias.gestion) : ""}</section>
    </div>
    <section class="tarjeta" style="margin-top:1rem"><div class="tarjeta-cuerpo"><h4>Remisiones abiertas con menor probabilidad de terminar efectivas</h4><p class="tenue">Las que conviene priorizar en la Oficina de Referencia.</p></div>
      <div class="tabla-scroll"><table class="ia-tabla"><thead><tr><th>Radicado</th><th>Paciente</th><th>Receptora</th><th>Intentos</th><th>Abierta</th><th>Pronóstico</th></tr></thead><tbody>
      ${d.mas_riesgo.map(a => `<tr class="clic" data-id="${a.id}"><td class="asiento-num">${esc(a.radicado)}</td><td>${esc(a.paciente_nombre)}<span class="sub">${esc(a.eps)}</span></td>
        <td>${esc(a.institucion_destino)}</td><td class="mono">${a.intentos}</td><td class="mono">${horasTxt(a.horas_abierta)}</td><td>${barraIA(a.probabilidad_efectiva)}</td></tr>`).join("")
        || `<tr><td colspan="6" class="tenue">No hay pronósticos todavía${u.rol === "admin" ? ": usa “Recalcular abiertas”." : "."}</td></tr>`}</tbody></table></div></section>
    <section class="tarjeta tarjeta-cuerpo" style="margin-top:1rem"><h4>Prueba el clasificador de respuestas</h4>
      <p class="tenue" style="margin-bottom:.6rem">Escribe una respuesta como la anotaría el equipo; la IA le pone la causa estándar.</p>
      <div class="buscar" style="max-width:560px">${icono("ia")}<input id="p-texto" aria-label="Respuesta de prueba" placeholder="Ej.: no hay neurocirujano de turno hasta mañana"></div>
      <p id="p-res" style="margin-top:.6rem" aria-live="polite"></p></section>`;
  $$("#p-cuerpo tr.clic").forEach(tr => tr.addEventListener("click", () => ir("ficha", { remision: +tr.dataset.id, desde: "pronosticos" })));
  $("#p-texto").addEventListener("input", debounce(async e => {
    const t = e.target.value.trim();
    if (t.length < 3) { $("#p-res").textContent = ""; return; }
    try {
      const k = await API.post("/registro/ia/clasificar", { texto: t });
      $("#p-res").innerHTML = `<span class="causa c-${esc(k.efecto)} ${esc(k.causa)}">${esc(k.etiqueta)}</span> <span class="tenue">· cuenta como ${esc(k.efecto)} · ${k.confianza == null ? esc(k.origen) : `confianza ${Math.round(100 * k.confianza)} % (${esc(k.origen)})`}</span>`;
    } catch (x) { $("#p-res").textContent = x.message; }
  }, 300));
}

// La sección de pronósticos va con las demás de IA (prediccion.js y agrupacion.js
// se registran antes que este archivo, así el grupo del menú queda junto).
if (typeof SECCIONES !== "undefined") {
  SECCIONES.pronosticos = { titulo: "Pronósticos de remisiones", icono: "ia", roles: ["admin", "medico", "especialista"],
    pintar: vistaPronosticos, grupo: "Inteligencia artificial" };
}
