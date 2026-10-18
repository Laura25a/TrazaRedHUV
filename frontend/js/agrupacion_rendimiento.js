// ==========================================================================
// Pestaña "Rendimiento" del tablero de agrupación (R07).
// Muestra las mismas tablas del cuaderno: rendimiento global y por grupo (celdas
// 125, 128 y 129), distribución KTAS (celda 120) y sintéticos reales vs. sintéticos
// (celda 136). Todo viene de GET /ia/clusters/detalle -> d.rendimiento, d.ktas, d.sinteticos
// Se carga DESPUÉS de agrupacion.js (usa agrColor y agrNum).
// ==========================================================================
"use strict";

function agrRendimiento(d) {
  const r = d.rendimiento, g = r.global;
  const pct = (v, dec = 1) => (v == null ? "—" : agrNum(v * 100, dec) + " %");
  const si = (b) => b ? `<span class="pildora est-pendiente">Sí</span>` : `<span class="pildora est-activo">No</span>`;
  const pval = (p) => (p == null ? "—" : p < 0.0001 ? "< 0,0001" : agrNum(p, 4));

  const filas = r.grupos.map(x => `<tr>
      <td><span class="agr-punto" style="background:${agrColor(x.cluster)}"></span><strong>Grupo ${x.cluster}</strong></td>
      <td class="agr-desc">${esc(x.nombre)}</td>
      <td class="num">${x.n}</td>
      <td class="num">${pct(x.sens, 1)}</td>
      <td class="num">${pct(x.espec, 1)}</td>
      <td class="num">${x.fn} <span class="tenue">(esp. ${agrNum(x.fn_esperados, 1)})</span></td>
      <td class="num">${x.fp} <span class="tenue">(esp. ${agrNum(x.fp_esperadas, 1)})</span></td>
      <td class="num">${pct(x.error, 1)}</td>
      <td>${si(x.zona_por_perdidos)}</td>
      <td>${si(x.zona_por_error)}</td>
      <td class="num" data-tip="Valor p de más urgentes perdidos de lo esperado">${pval(x.p_perdidos)}</td>
      <td class="num" data-tip="Valor p de más falsas alarmas de lo esperado">${pval(x.p_falsas_alarmas)}</td>
      <td>${x.zona_confusion
        ? `<span class="pildora est-pendiente">${icono("alerta")}Zona de confusión</span>`
        : `<span class="pildora est-activo">${icono("check")}Sin alerta</span>`}</td>
    </tr>`).join("");

  const tablaRend = `<div class="tabla-scroll"><table>
    <thead><tr><th>Grupo</th><th>Perfil</th><th>n</th><th>Sensibilidad</th><th>Especificidad</th>
      <th>Urgentes perdidos (FN)</th><th>Falsas alarmas (FP)</th><th>Error</th>
      <th>Regla simple: sensibilidad menor que la global</th><th>Regla simple: error mayor que el global</th>
      <th>p perdidos</th><th>p falsas alarmas</th><th>Zona (prueba estadística)</th></tr></thead>
    <tbody>${filas}
      <tr class="agr-cohorte"><td><strong>Global</strong></td><td>Todos los pacientes reales</td><td class="num">${g.n}</td>
        <td class="num">${pct(g.sens, 1)}</td><td class="num">${pct(g.espec, 1)}</td>
        <td class="num">${g.fn}</td><td class="num">${g.fp}</td><td class="num">${pct(g.error, 1)}</td>
        <td colspan="5"></td></tr>
    </tbody></table></div>`;

  const tablaKtas = `<div class="tabla-scroll"><table>
    <thead><tr><th>Grupo</th><th>KTAS 1</th><th>KTAS 2</th><th>KTAS 3</th><th>KTAS 4</th><th>KTAS 5</th>
      <th>% urgentes (KTAS 1-3)</th><th>% KTAS 1-2</th></tr></thead>
    <tbody>${d.ktas.map(x => `<tr>
      <td><span class="agr-punto" style="background:${agrColor(x.cluster)}"></span><strong>Grupo ${x.cluster}</strong></td>
      ${["1", "2", "3", "4", "5"].map(k => `<td class="num">${agrNum(x.dist[k], 2)}</td>`).join("")}
      <td class="num">${agrNum(x.pct_urgentes, 1)} %</td><td class="num">${agrNum(x.pct_ktas12, 1)} %</td></tr>`).join("")}
    </tbody></table></div>`;

  const s = d.sinteticos, vs = s.variables;
  const tablaSint = `<div class="tabla-scroll"><table>
    <thead><tr><th>Grupo</th><th>Sintéticos</th><th>Reconocidos en su grupo</th>
      ${vs.map(v => `<th>${esc(v.nombre)} (real / sint.)</th>`).join("")}</tr></thead>
    <tbody>${s.grupos.map(x => `<tr>
      <td><span class="agr-punto" style="background:${agrColor(x.cluster)}"></span><strong>Grupo ${x.cluster}</strong></td>
      <td class="num">${x.n}</td><td class="num">${pct(x.reconocidos, 1)}</td>
      ${vs.map(v => `<td class="num">${agrNum(x.real[v.id], 1)} / ${agrNum(x.sintetico[v.id], 1)}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>`;

  return `
    <div class="agr-nota">${icono("info")}<p>
      <strong>¿Para qué sirve?</strong> Ver dónde se equivoca el modelo de urgencia (XGBoost, umbral ${agrNum(d.umbral, 2)},
      validación cruzada de 5 particiones sobre los ${g.n} pacientes reales). Un grupo entra en <em>zona de confusión</em>
      cuando tiene más urgentes perdidos o más falsas alarmas de lo esperado según el desempeño global, con una prueba binomial
      (nivel ${agrNum(r.alfa, 4)} tras la corrección por comparaciones múltiples). Las dos columnas de «regla simple» son
      solo informativas.</p></div>
    <div class="agr-global">Global: sensibilidad <strong>${agrNum(g.sens, 3)}</strong> · especificidad <strong>${agrNum(g.espec, 3)}</strong>
      · error <strong>${agrNum(g.error, 3)}</strong></div>
    <h4 class="agr-subtitulo">Rendimiento del modelo por grupo</h4>${tablaRend}
    <h4 class="agr-subtitulo">Distribución KTAS por grupo</h4>${tablaKtas}
    <h4 class="agr-subtitulo">Pacientes sintéticos frente a los reales</h4>
    <p class="tenue">${agrNum(s.global_reconocidos * 100, 1)} % de los sintéticos se reconoce en su grupo ·
      valores fuera del rango real: ${s.fuera_de_rango}. Los sintéticos nunca se usan para entrenar ni evaluar el modelo.</p>
    ${tablaSint}`;
}
