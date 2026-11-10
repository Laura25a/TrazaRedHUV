// ==========================================================================
// TrazaRed HUV — Tendencias de la red (semana 10: tablero de POBLACIÓN)
// ==========================================================================
// Para el administrador y el director: series semanales agregadas, sin
// identificar pacientes. GET /tablero/tendencia?semanas=N
// Cada gráfica responde una pregunta de gestión (ver el texto bajo el título).
// El director entra directo aquí: no ve el libro ni fichas individuales (403).
"use strict";

let TAB_SEMANAS = 12;

async function vistaTendencia() {
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Tendencias de la red</h2>
      <p>Cómo evoluciona semana a semana el trabajo de remisión, la IA y las alertas. Datos agregados: no identifican pacientes.</p></div>
      <label class="tab-semanas">Últimas
        <select id="tab-sem">${[4, 8, 12, 26, 52].map(n => `<option value="${n}" ${n === TAB_SEMANAS ? "selected" : ""}>${n} semanas</option>`).join("")}</select></label></div>
    <div id="tab-cuerpo">${cargando()}</div></div>`;
  $("#tab-sem").addEventListener("change", (e) => { TAB_SEMANAS = +e.target.value; vistaTendencia(); });

  let d;
  try { d = await API.get(`/tablero/tendencia?semanas=${TAB_SEMANAS}`); }
  catch (e) { $("#tab-cuerpo").innerHTML = `<div class="tarjeta vacio">${esc(e.status === 403 ? "Tu rol no tiene acceso al tablero de dirección." : e.message)}</div>`; return; }

  const t = d.totales || {};
  const suma = (serie, k) => serie.reduce((a, f) => a + (f[k] || 0), 0);
  const rem = d.remisiones, rep = d.reportes_ia, al = d.alertas;
  $("#tab-cuerpo").innerHTML = `
    <div class="kpis">
      ${kpi("libro", "tono-azul", suma(rem, "total"), `Remisiones en ${TAB_SEMANAS} semanas`)}
      ${kpi("check", "tono-verde", `${pct(suma(rem, "efectivas"), suma(rem, "total"))}%`, "Remisiones efectivas")}
      ${kpi("prediccion", "tono-violeta", t.reportes_pendientes ?? "—", "Reportes de IA sin revisar")}
      ${kpi("alerta", "tono-rojo", t.alertas_activas ?? "—", "Alertas críticas activas")}
      ${kpi("asistente", "tono-azul", `${t.propuestas_agente_aprobadas ?? 0} / ${t.propuestas_agente_rechazadas ?? 0}`, "Propuestas del asistente aprobadas / rechazadas")}
    </div>
    <div class="graficas">
      ${tarjetaGrafica("Remisiones por semana", "¿Está creciendo la demanda y cuántas terminan en traslado efectivo?",
        tabBarrasApiladas(rem, [["efectivas", "Efectivas", "var(--ok)"], ["rechazadas", "Rechazadas", "var(--error)"]], "total"))}
      ${tarjetaGrafica("Reportes de IA por semana", "¿Los médicos revisan a tiempo lo que propone el modelo?",
        tabBarrasApiladas(rep, [["aprobados", "Aprobados", "var(--ok)"], ["rechazados", "Rechazados", "var(--error)"], ["pendientes", "Pendientes", "var(--alerta)"]], "total"))}
      ${tarjetaGrafica("Alertas críticas por semana", "¿Alguna alerta tuvo que escalarse al administrador por no atenderse en 10 minutos?",
        tabBarrasApiladas(al, [["reconocidas", "Reconocidas", "var(--ok)"], ["escaladas", "Escaladas", "var(--error)"]], "total"))}
      ${tarjetaGrafica("Minutos hasta reconocer una alerta", "Promedio semanal: más bajo es mejor",
        barrasV(al.filter(f => f.minutos_hasta_reconocer != null).map(f => ({ etiqueta: tabSemana(f.semana), valor: Math.round(f.minutos_hasta_reconocer) }))))}
    </div>
    <p class="tenue" style="margin-top:1rem">${esc(d.nota)}</p>`;
  activarTooltips($("#tab-cuerpo"));
}

const tabSemana = (iso) => { const [, m, dd] = iso.split("-"); return `${dd}/${m}`; };

function tabBarrasApiladas(serie, partes, claveTotal) {
  if (!serie.length) return { svg: `<div class="vacio">Sin datos en este periodo.</div>`, tabla: "" };
  const W = 560, H = 240, base = 200, top = 18, izq = 10;
  const max = Math.max(...serie.map(f => f[claveTotal] || 0), 1);
  const paso = (W - izq * 2) / serie.length, bw = Math.min(40, paso - 6);
  const svg = `<svg viewBox="0 0 ${W} ${H}" role="img"><line class="eje" x1="0" x2="${W}" y1="${base}" y2="${base}"/>
    ${serie.map((f, i) => {
      const x = izq + i * paso + (paso - bw) / 2;
      let y = base, rects = "";
      const total = f[claveTotal] || 0;
      for (const [k, nombre, color] of partes) {
        const h = ((base - top) * (f[k] || 0)) / max;
        y -= h;
        if (h > 0) rects += `<rect class="marca-dato" x="${x}" y="${y}" width="${bw}" height="${h}" fill="${color}" data-tip="Semana del ${tabSemana(f.semana)} · ${nombre}: ${f[k]} de ${total}"/>`;
      }
      const resto = ((base - top) * total) / max - (base - y);
      if (resto > 0.5) rects += `<rect class="marca-dato" x="${x}" y="${y - resto}" width="${bw}" height="${resto}" fill="var(--azul-borde)" data-tip="Semana del ${tabSemana(f.semana)} · otros: ${Math.round(total - partes.reduce((a, [k]) => a + (f[k] || 0), 0))}"/>`;
      return rects + `<text class="val" x="${x + bw / 2}" y="${base - ((base - top) * total) / max - 5}" text-anchor="middle">${total}</text>
        <text class="etq" x="${x + bw / 2}" y="${base + 16}" text-anchor="middle">${tabSemana(f.semana)}</text>`;
    }).join("")}
    ${partes.map(([, nombre, color], i) => `<rect x="${izq + i * 120}" y="${H - 14}" width="10" height="10" fill="${color}"/><text class="etq" x="${izq + 14 + i * 120}" y="${H - 5}">${esc(nombre)}</text>`).join("")}
  </svg>`;
  const tabla = `<table style="margin-top:.5rem"><thead><tr><th>Semana</th><th>Total</th>${partes.map(([, n]) => `<th>${esc(n)}</th>`).join("")}</tr></thead>
    <tbody>${serie.map(f => `<tr><td>${tabSemana(f.semana)}</td><td class="num">${f[claveTotal]}</td>${partes.map(([k]) => `<td class="num">${f[k] ?? 0}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  return { svg, tabla };
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.tendencia = '<path d="M3 3v18h18"/><path d="m7 15 4-4 3 3 5-6"/>';
  SECCIONES.tendencia = {
    titulo: "Tendencias de la red", icono: "tendencia",
    roles: ["admin", "director"],
    pintar: vistaTendencia, grupo: "Dirección",
  };
  // El director ve la población, no el libro: su "inicio" es este tablero
  if (typeof ROLES !== "undefined") ROLES.director = "Dirección";
  const inicioOriginal = SECCIONES.inicio.pintar;
  SECCIONES.inicio.pintar = (p) => (sesion.usuario && sesion.usuario.rol === "director" ? vistaTendencia(p) : inicioOriginal(p));
  if (SECCIONES.agrupacion) SECCIONES.agrupacion.roles = [...new Set([...SECCIONES.agrupacion.roles, "director"])];
}
