// ==========================================================================
// TrazaRed HUV — tablero de agrupación clínica (R07, Persona 2)
// ==========================================================================
// Muestra los grupos de pacientes que calcula el servicio de IA (K-means) y
// dónde se equivoca el modelo supervisado (zona de confusión). Datos:
//   GET /ia/clusters          -> resumen por grupo (reales, sintéticos, error)
//   GET /ia/clusters/detalle  -> perfiles, mapa PCA, calidad (k = 2…8),
//                                rendimiento del modelo, KTAS y sintéticos
//
// NO se modificó app.js: este módulo registra su sección en SECCIONES (el
// menú se arma después del login) y reutiliza kpi(), barrasH(), tarjetaGrafica(),
// activarTooltips() y los estilos que ya existen en app.js / estilos.css.
// Todo número y toda frase interpretativa se calculan con los datos que llegan,
// para que nada quede desactualizado si se reentrena.
// La pestaña "Rendimiento" se dibuja con agrRendimiento(), de agrupacion_rendimiento.js.
"use strict";

const AGR_COLORES = ["#1f63c6", "#d98a09", "#1a9660", "#5b4bb7", "#c83a2e", "#2bb3e6", "#8594a8", "#0d2b5b"];
const agrColor = (i) => AGR_COLORES[i % AGR_COLORES.length];
const agrNum = (v, d = 2) => (v == null ? "—" : Number(v).toLocaleString("es-CO", { maximumFractionDigits: d }));

async function vistaAgrupacion() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Agrupación clínica</h2>
      <p>Grupos de pacientes con características parecidas y zonas donde el modelo de urgencia (KTAS) se equivoca más.</p></div></div>
    <section class="tarjeta">
      <div class="pestanas" id="agr-tabs">
        <button class="activa" data-tab="resumen">Resumen</button>
        <button data-tab="mapa">Mapa de pacientes</button>
        <button data-tab="perfiles">Perfiles</button>
        <button data-tab="rendimiento">Rendimiento</button>
        <button data-tab="calidad">Calidad del modelo</button>
      </div>
      <div class="agr-panel" id="agr">${cargando("Calculando grupos…")}</div>
    </section></div>`;

  let grupos, detalle = null;
  try {
    grupos = await API.get("/ia/clusters");
  } catch (e) {
    $("#agr").innerHTML = `<div class="vacio">${esc(e.status === 403
      ? "Tu rol no tiene acceso a la agrupación clínica." : e.message)}</div>`;
    return;
  }
  if (!Array.isArray(grupos) || !grupos.length) {
    $("#agr").innerHTML = `<div class="vacio">El servicio de IA no devolvió grupos.</div>`;
    return;
  }

  const pintarTab = {
    resumen: () => agrResumen(grupos),
    mapa: (d) => agrMapa(d, grupos),
    perfiles: (d) => agrPerfiles(d, grupos),
    rendimiento: (d) => agrRendimiento(d),
    calidad: (d) => agrCalidad(d),
  };
  async function abrir(tab) {
    $$("#agr-tabs button").forEach(b => b.classList.toggle("activa", b.dataset.tab === tab));
    const panel = $("#agr");
    if (tab !== "resumen" && !detalle) {
      panel.innerHTML = cargando("Calculando mapa, perfiles y calidad…");
      try { detalle = await API.get("/ia/clusters/detalle"); }
      catch (e) { panel.innerHTML = `<div class="vacio">${esc(e.message)}</div>`; return; }
    }
    panel.innerHTML = pintarTab[tab](detalle);
    activarTooltips(panel);
    if (tab === "mapa") agrEnlazarMapa(detalle, grupos);
  }
  $$("#agr-tabs button").forEach(b => b.addEventListener("click", () => abrir(b.dataset.tab)));
  abrir("resumen");
}

// ---------------------------------------------------------------- Resumen
function agrResumen(grupos) {
  const reales = grupos.reduce((s, g) => s + g.n_reales, 0);
  const sinteticos = grupos.reduce((s, g) => s + g.n_sinteticos, 0);
  const enConfusion = grupos.filter(g => g.zona_confusion);
  const nombre = g => `Grupo ${g.cluster}`;
  const perfil = g => String(g.descripcion || "").split(". ")[0];

  const datosReales = grupos.map(g => ({ etiqueta: nombre(g), valor: g.n_reales, n: g.n_sinteticos }));
  const datosError = grupos.map(g => ({ etiqueta: nombre(g), valor: +(g.tasa_error_modelo * 100).toFixed(1), sufijo: "%", n: g.n_reales }));
  const datosSint = grupos.map(g => ({ etiqueta: nombre(g), valor: +(g.sinteticos_reconocidos * 100).toFixed(1), sufijo: "%", n: g.n_sinteticos }));

  const tabla = `<div class="tabla-scroll"><table>
    <thead><tr><th>Grupo</th><th>Perfil</th><th>Reales</th><th>Sintéticos</th><th>Error del modelo</th><th>Estado</th></tr></thead>
    <tbody>${grupos.map(g => `<tr>
      <td><span class="agr-punto" style="background:${agrColor(g.cluster)}"></span><strong>${esc(nombre(g))}</strong></td>
      <td class="agr-desc">${esc(perfil(g))}<span class="sub">${esc(String(g.descripcion || "").slice(perfil(g).length + 2))}</span></td>
      <td class="num">${g.n_reales}</td>
      <td class="num">${g.n_sinteticos}</td>
      <td class="num">${(g.tasa_error_modelo * 100).toFixed(1)} %</td>
      <td>${g.zona_confusion
        ? `<span class="pildora est-pendiente">${icono("alerta")}Zona de confusión</span>`
        : `<span class="pildora est-activo">${icono("check")}Sin alerta</span>`}</td>
    </tr>`).join("")}</tbody></table></div>`;

  return `
    <div class="kpis" style="margin-bottom:1.1rem">
      ${kpi("pacientes", "tono-azul", reales, "Pacientes reales")}
      ${kpi("trazo", "tono-violeta", sinteticos, "Pacientes sintéticos")}
      ${kpi("agrupacion", "tono-verde", grupos.length, "Grupos")}
      ${kpi("alerta", enConfusion.length ? "tono-ambar" : "tono-verde", enConfusion.length, "Grupos en zona de confusión")}
    </div>
    <div class="agr-nota">${icono("info")}<p>
      <strong>¿Para qué sirve?</strong> Los grupos orientan, no deciden. En un grupo en <em>zona de confusión</em> el modelo
      se equivoca más de lo esperado: conviene pedir una confirmación clínica antes de decidir.
      Los pacientes sintéticos son datos simulados a partir de cada grupo (no son personas reales) y no se usan para evaluar el modelo.</p></div>
    <div class="graficas">
      ${tarjetaGrafica("Pacientes reales por grupo", "Número de pacientes de cada grupo", barrasH(datosReales, 0))}
      ${tarjetaGrafica("Error del modelo por grupo", "% de pacientes del grupo mal clasificados (validación cruzada)", barrasH(datosError, 0))}
      ${tarjetaGrafica("Sintéticos reconocidos por su grupo", "% de sintéticos que el K-means vuelve a asignar al grupo donde se crearon", barrasH(datosSint, 100))}
    </div>
    <h4 class="agr-subtitulo">Qué es cada grupo</h4>${tabla}`;
}

// ---------------------------------------------------------------- Mapa PCA
function agrMapa(d, grupos) {
  return `
    <div class="agr-nota">${icono("info")}<p>
      <strong>¿Para qué sirve?</strong> Cada punto es un paciente, proyectado en dos dimensiones (PCA). Cambia el color para ver si los grupos
      se separan y dónde caen los pacientes urgentes. Si los urgentes se reparten en varios grupos, la ruta de atención no puede depender solo del grupo.</p></div>
    <div class="agr-controles">
      <label>Colorear por
        <select id="agr-color"><option value="grupo">Grupo</option><option value="urgente">Urgencia real (KTAS ≤ 3)</option></select></label>
      <label class="check"><input type="checkbox" id="agr-sint" checked> Mostrar sintéticos</label>
      <span class="tenue">PC1 explica ${agrNum(d.mapa.varianza[0], 1)} % y PC2 ${agrNum(d.mapa.varianza[1], 1)} % de la variación</span>
    </div>
    <div class="grafica" id="agr-mapa"></div>`;
}

function agrEnlazarMapa(d, grupos) {
  const reales = d.mapa.reales, sint = d.mapa.sinteticos;
  const todos = reales.concat(sint);
  const xs = todos.map(p => p[0]), ys = todos.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const W = 720, H = 430, m = { l: 46, r: 14, t: 12, b: 38 };
  const X = v => m.l + ((v - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = v => H - m.b - ((v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b);

  function dibujar() {
    const modo = $("#agr-color").value, conSint = $("#agr-sint").checked;
    const color = (c, u) => modo === "grupo" ? agrColor(c) : (u ? "#c83a2e" : "#1f63c6");
    const leyenda = modo === "grupo"
      ? grupos.map(g => `<span><i style="background:${agrColor(g.cluster)}"></i>Grupo ${g.cluster}</span>`).join("")
      : `<span><i style="background:#c83a2e"></i>Urgente (KTAS ≤ 3)</span><span><i style="background:#1f63c6"></i>No urgente</span>`;
    const pts = reales.map(([x, y, c, u]) =>
      `<circle class="marca-dato" cx="${X(x).toFixed(1)}" cy="${Y(y).toFixed(1)}" r="3.6" fill="${color(c, u)}" fill-opacity=".72"
        data-tip="Grupo ${c} · ${u ? "urgente" : "no urgente"}"/>`).join("");
    const sin = conSint ? sint.map(([x, y, c]) =>
      `<circle class="marca-dato" cx="${X(x).toFixed(1)}" cy="${Y(y).toFixed(1)}" r="4.2" fill="none" stroke="${modo === "grupo" ? agrColor(c) : "#55657b"}" stroke-width="1.4"
        data-tip="Sintético · grupo ${c}"/>`).join("") : "";
    $("#agr-mapa").innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Mapa PCA de pacientes">
        <line class="eje" x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}"/>
        <line class="eje" x1="${m.l}" x2="${m.l}" y1="${m.t}" y2="${H - m.b}"/>
        <text class="etq" x="${(W + m.l) / 2}" y="${H - 8}" text-anchor="middle">PC1 (${agrNum(d.mapa.varianza[0], 1)} %)</text>
        <text class="etq" x="14" y="${(H - m.b + m.t) / 2}" text-anchor="middle" transform="rotate(-90 14 ${(H - m.b + m.t) / 2})">PC2 (${agrNum(d.mapa.varianza[1], 1)} %)</text>
        ${pts}${sin}</svg>
      <div class="leyenda" style="margin-top:.6rem">${leyenda}${conSint ? `<span><i class="agr-hueco"></i>Sintético (círculo vacío)</span>` : ""}</div>`;
    activarTooltips($("#agr-mapa"));
  }
  $("#agr-color").addEventListener("change", dibujar);
  $("#agr-sint").addEventListener("change", dibujar);
  dibujar();
}

// ---------------------------------------------------------------- Perfiles
function agrPerfiles(d, grupos) {
  const vars = d.variables, perfiles = d.perfiles;
  // Color de la celda: azul si queda por debajo del promedio, rojo si por encima
  const celda = (dev) => {
    if (dev == null) return "";
    const a = Math.min(Math.abs(dev) / 1.2, 1) * 0.55;
    return `background:${dev < 0 ? `rgba(31,99,198,${a})` : `rgba(200,58,46,${a})`}`;
  };
  const tabla = `<div class="tabla-scroll"><table class="agr-calor">
    <thead><tr><th>Grupo</th>${vars.map(v => `<th>${esc(v.nombre)}</th>`).join("")}<th>n</th></tr></thead>
    <tbody>${perfiles.map(p => `<tr><td><span class="agr-punto" style="background:${agrColor(p.cluster)}"></span><strong>Grupo ${p.cluster}</strong></td>
      ${vars.map(v => `<td class="num" style="${celda(p.desviaciones[v.id])}" data-tip="${esc(v.nombre)}: ${agrNum(p.desviaciones[v.id])} desv. est. respecto a la cohorte">${agrNum(p.promedios[v.id])}</td>`).join("")}
      <td class="num">${p.n}</td></tr>`).join("")}
      <tr class="agr-cohorte"><td><strong>Cohorte</strong></td>${vars.map(v => `<td class="num">${agrNum(d.cohorte[v.id])}</td>`).join("")}<td class="num">${perfiles.reduce((s, p) => s + p.n, 0)}</td></tr>
    </tbody></table></div>`;

  // Distancia al promedio: una fila por variable, una barra por grupo
  const W = 640, izq = 170, alto = 14, sep = 12, filaH = perfiles.length * alto + sep;
  const H = vars.length * filaH + 10, ancho = W - izq - 20, maxAbs = Math.max(0.5, ...perfiles.flatMap(p => vars.map(v => Math.abs(p.desviaciones[v.id] || 0))));
  const cx = izq + ancho / 2, esc_ = (ancho / 2) / maxAbs;
  const barras = vars.map((v, i) => {
    const y0 = 6 + i * filaH;
    return `<text class="etq" x="${izq - 10}" y="${y0 + (perfiles.length * alto) / 2 + 4}" text-anchor="end">${esc(v.nombre)}</text>` +
      perfiles.map((p, j) => {
        const dv = p.desviaciones[v.id] || 0, w = Math.abs(dv) * esc_;
        return `<rect class="marca-dato" x="${dv < 0 ? cx - w : cx}" y="${y0 + j * alto}" width="${Math.max(w, 1)}" height="${alto - 3}" rx="2" fill="${agrColor(p.cluster)}"
          data-tip="Grupo ${p.cluster} · ${esc(v.nombre)}: ${agrNum(dv)} desv. est."/>`;
      }).join("");
  }).join("");
  const leyenda = perfiles.map(p => `<span><i style="background:${agrColor(p.cluster)}"></i>Grupo ${p.cluster}</span>`).join("");

  return `
    <div class="agr-nota">${icono("info")}<p>
      <strong>¿Para qué sirve?</strong> Ver qué caracteriza a cada grupo para diseñar rutas de atención distintas.
      El color indica cuánto se aleja del promedio de la cohorte: <b>azul</b> por debajo, <b>rojo</b> por encima.</p></div>
    <h4 class="agr-subtitulo">Promedio de cada grupo</h4>${tabla}
    <h4 class="agr-subtitulo">Distancia al promedio (desviaciones estándar)</h4>
    <div class="grafica"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Distancia al promedio por variable">
      <line class="eje" x1="${cx}" x2="${cx}" y1="0" y2="${H}"/>${barras}</svg>
      <div class="leyenda" style="margin-top:.6rem">${leyenda}</div></div>`;
}

// ---------------------------------------------------------------- Calidad
function agrLinea(titulo, sub, serie, clave, kServicio, formato) {
  const W = 520, H = 250, m = { l: 52, r: 16, t: 14, b: 34 };
  const vals = serie.map(s => s[clave]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const X = i => m.l + (i / (serie.length - 1 || 1)) * (W - m.l - m.r);
  const Y = v => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  const linea = serie.map((s, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(s[clave]).toFixed(1)}`).join(" ");
  const marcas = serie.map((s, i) => {
    const es = s.k === kServicio;
    return `<circle class="marca-dato" cx="${X(i).toFixed(1)}" cy="${Y(s[clave]).toFixed(1)}" r="${es ? 7 : 4}" fill="var(--azul)"
        data-tip="k = ${s.k}: ${formato(s[clave])}${es ? " (k en servicio)" : ""}"/>
      <text class="etq" x="${X(i).toFixed(1)}" y="${H - 10}" text-anchor="middle">k = ${s.k}</text>`;
  }).join("");
  const ticks = [0, 1, 2, 3].map(t => { const v = lo + ((hi - lo) * t) / 3; return `<text class="etq" x="${m.l - 8}" y="${(Y(v) + 4).toFixed(1)}" text-anchor="end">${formato(v)}</text>
      <line class="eje" x1="${m.l}" x2="${W - m.r}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke-dasharray="2 4"/>`; }).join("");
  return `<section class="tarjeta tarjeta-cuerpo grafica"><h4>${esc(titulo)}</h4><p class="tenue">${esc(sub)}</p>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(titulo)}">${ticks}<path d="${linea}" fill="none" stroke="var(--azul)" stroke-width="2"/>${marcas}</svg></section>`;
}

function agrCalidad(d) {
  const actual = d.calidad.find(c => c.k === d.k_servicio);
  const mejor = d.calidad.reduce((a, b) => (b.silueta > a.silueta ? b : a));
  const sil = actual ? actual.silueta : null;
  // La interpretación sale de los números que llegan (no queda desactualizada)
  const lectura = sil == null ? "" : sil >= 0.5 ? "los grupos están bien separados."
    : sil >= 0.25 ? "la separación es moderada: los grupos se tocan en parte."
    : "los grupos se tocan mucho: el tablero orienta, no decide.";
  return `
    <div class="agr-nota">${icono("info")}<p>
      <strong>¿Para qué sirve?</strong> Auditar el agrupamiento. El punto grande es el número de grupos en servicio (k = ${d.k_servicio}).
      Con k = ${d.k_servicio} la silueta es <strong>${agrNum(sil, 3)}</strong>: ${esc(lectura)}
      ${mejor.k !== d.k_servicio ? `La silueta más alta es con k = ${mejor.k} (${agrNum(mejor.silueta, 3)}). ` : ""}
      Si al reentrenar con datos nuevos estas curvas cambian mucho, hay que revisar los grupos antes de seguir usándolos.</p></div>
    <div class="graficas">
      ${agrLinea("Codo: inercia (menor = grupos más compactos)", "Suma de distancias de cada paciente a su centro", d.calidad, "inercia", d.k_servicio, v => agrNum(v, 0))}
      ${agrLinea("Silueta (mayor = grupos más separados)", "Va de −1 a 1; cerca de 0 los grupos se superponen", d.calidad, "silueta", d.k_servicio, v => agrNum(v, 2))}
    </div>`;
}

// Registro en el menú (se arma después del login, así que basta con hacerlo aquí).
if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.agrupacion = '<circle cx="7" cy="8" r="3"/><circle cx="17" cy="8" r="3"/><circle cx="12" cy="17" r="3"/>';
  SECCIONES.agrupacion = {
    titulo: "Agrupación clínica", icono: "agrupacion",
    roles: ["admin", "medico", "especialista"],     // igual que /ia/clusters en la API
    pintar: vistaAgrupacion, grupo: "Inteligencia artificial",
  };
}