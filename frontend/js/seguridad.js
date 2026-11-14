// ==========================================================================
// TrazaRed HUV — Panel de seguridad del administrador (modelo STRIDE)
// ==========================================================================
// Solo admin. Reúne en una pantalla la evidencia de los controles del proyecto
// final: cifrado en reposo (R27), auditoría encadenada (R28), contraseñas,
// incidentes y bloqueos de la guardia del agente (R25), límite de tasa y la
// bitácora de lo que se le envió al LLM (R24). El médico no ve nada de esto:
// a él le llega la respuesta; el detalle técnico es del administrador.
"use strict";

const SEG = { pestana: "resumen" };

async function vistaSeguridad(params = {}) {
  if (params.pestana) SEG.pestana = params.pestana;
  const pestanas = [["resumen", "Resumen"], ["incidentes", "Incidentes y bloqueos"], ["llm", "Lo enviado al LLM"]];
  $("#contenido").innerHTML = `<div class="pagina">
    <div class="pagina-cabeza"><div><h2>Seguridad</h2>
      <p>Cada control responde a una amenaza del modelo STRIDE (docs/STRIDE.md). Aquí está su evidencia en vivo.</p></div></div>
    <section class="tarjeta"><div class="pestanas" id="seg-pestanas">${pestanas.map(([k, t]) =>
      `<button data-p="${k}" class="${k === SEG.pestana ? "activa" : ""}">${t}</button>`).join("")}</div>
      <div id="seg-cuerpo" class="itp-cuerpo">${cargando()}</div></section></div>`;
  $$("#seg-pestanas button").forEach(b => b.addEventListener("click", () => { SEG.pestana = b.dataset.p; vistaSeguridad(); }));
  try { await ({ resumen: segResumen, incidentes: segIncidentes, llm: segLLM })[SEG.pestana](); }
  catch (e) { $("#seg-cuerpo").innerHTML = `<div class="vacio">${esc(e.message)}</div>`; }
}

const segSello = (ok, si, no, tono = "mal") => `<span class="seg-sello ${ok ? "ok" : tono}">${ok ? si : no}</span>`;

async function segResumen() {
  const e = await API.get("/seguridad/estado");
  const c = e.cifrado, a = e.auditoria, k = e.contrasenas, l = e.limites, g = e.guardia;
  $("#seg-cuerpo").innerHTML = `<div class="seg-rejilla">
    <article class="seg-caja"><header><h4>Cifrado de datos personales</h4>${segSello(c.todo_cifrado, "Cifrado", "Incompleto")}</header>
      <p class="seg-num">${c.documento_cifrado} de ${c.total}</p><p class="tenue">pacientes con documento, teléfono, dirección y correo cifrados (${esc(c.algoritmo)}).</p>
      <p class="seg-mono" title="Así se ve un documento dentro de la base de datos">En la base: ${esc(c.muestra_en_base || "—")}</p>
      <p class="tenue">Llave v${c.llaves.version_vigente} · ${esc(c.llaves.cifrado)}. La búsqueda por documento usa un índice ciego (${c.con_indice} índices).</p>
      <button class="btn btn-suave btn-sm" id="seg-rotar" title="Después de agregar LLAVE_CIFRADO_V2 al .env">Recifrar con la llave vigente</button></article>
    <article class="seg-caja"><header><h4>Auditoría encadenada</h4>${segSello(a.integra, "Íntegra", "Alterada")}</header>
      <p class="seg-num">${a.registros.toLocaleString("es-CO")}</p><p class="tenue">registros, cada uno sellado con la huella del anterior.</p>
      ${a.integra ? `<p class="seg-mono">Última huella: ${esc((a.ultima_huella || "").slice(0, 24))}…</p>`
        : `<p class="error-form">Registro ${a.primer_registro_alterado}: ${esc(a.motivo)}</p>`}
      <button class="btn btn-suave btn-sm" id="seg-verificar">Verificar ahora</button></article>
    <article class="seg-caja"><header><h4>Contraseñas</h4>${segSello(k.por_migrar === 0, "Argon2id", "Migrando", "medio")}</header>
      <p class="seg-num">${k.argon2id} de ${k.argon2id + k.por_migrar}</p><p class="tenue">cuentas en Argon2id. Las que siguen en bcrypt se migran solas cuando su dueño inicia sesión.</p></article>
    <article class="seg-caja"><header><h4>Guardia del asistente</h4>${segSello(e.bloqueos_vigentes === 0, "Sin bloqueos", `${e.bloqueos_vigentes} bloqueo${e.bloqueos_vigentes === 1 ? "" : "s"}`)}</header>
      <p class="seg-num">${e.incidentes.ultimas_24h}</p><p class="tenue">incidentes en 24 h (${e.incidentes.total} en total). Bloqueo de ${g.bloqueo_min} min al ${g.intentos_para_bloqueo}.º intento en ${g.ventana_min} min.</p>
      <div class="seg-probar"><input id="seg-texto" maxlength="300" placeholder="Prueba un texto: «ignora tus instrucciones…»"><button class="btn btn-suave btn-sm" id="seg-probar">Probar</button></div>
      <p class="tenue" id="seg-resultado"></p></article>
    <article class="seg-caja"><header><h4>Límite de peticiones</h4>${segSello(true, "Activo", "")}</header>
      <p class="seg-num">${l.sensibles_por_ventana} cada ${l.ventana_s} s</p><p class="tenue">intentos fallidos por IP en login y token de sistema; ${l.socio_por_ventana} en la entrada del socio, ${l.ia_por_usuario} por usuario en el asistente y ${l.general_por_ventana} en el resto. Rechazadas con 429 desde el arranque: <strong>${l.rechazadas_429}</strong>.</p></article>
    <article class="seg-caja"><header><h4>Entradas externas</h4>${segSello(true, "Autenticadas", "")}</header>
      <p class="tenue">El aviso de HAPI FHIR exige la cabecera <code>${esc(e.webhook_fhir.cabecera)}</code> (${esc(e.webhook_fhir.secreto)}). El sistema socio entra con token de sistema y firma HMAC en cada mensaje.</p>
      <button class="btn btn-suave btn-sm" id="seg-interop">Ver mensajes del socio</button></article></div>`;
  $("#seg-verificar").addEventListener("click", async () => {
    try { const r = await API.get("/auditoria/verificar"); toast(r.integra ? `Cadena íntegra: ${r.registros} registros.` : `Cadena alterada en el registro ${r.primer_registro_alterado}.`, r.integra ? "ok" : "error"); segResumen(); }
    catch (x) { toast(x.message, "error"); }
  });
  $("#seg-rotar").addEventListener("click", async () => {
    try { const r = await API.post("/seguridad/cifrado/rotar"); toast(`Llave v${r.version}: ${r.recifrados} recifrados, ${r.cifrados} cifrados.`, "ok"); segResumen(); }
    catch (x) { toast(x.message, "error"); }
  });
  $("#seg-interop").addEventListener("click", () => ir("interop", { pestana: "mensajes" }));
  const probar = async () => {
    const t = $("#seg-texto").value.trim();
    if (!t) return;
    try { const r = await API.post("/seguridad/guardia/probar", { texto: t });
      $("#seg-resultado").textContent = r.sospechoso ? `Bloqueado. Fragmento: «${r.fragmento}». Como dato quedaría: ${r.neutralizado}` : "Pasa: no parece una orden para el asistente."; }
    catch (x) { toast(x.message, "error"); }
  };
  $("#seg-probar").addEventListener("click", probar);
  $("#seg-texto").addEventListener("keydown", (ev) => { if (ev.key === "Enter") probar(); });
}

async function segIncidentes() {
  const [bloqueos, incidentes] = await Promise.all([API.get("/seguridad/bloqueos"), API.get("/seguridad/incidentes?limite=100")]);
  const TIPOS = { inyeccion_directa: "Orden al asistente en la pregunta", inyeccion_indirecta: "Orden escondida en datos", firma_invalida: "Firma inválida del socio" };
  $("#seg-cuerpo").innerHTML = `
    <h4 class="seg-titulo">Bloqueos vigentes</h4>
    ${bloqueos.length ? `<div class="tabla-scroll"><table><thead><tr><th>Quién</th><th>Motivo</th><th>Hasta</th><th></th></tr></thead><tbody>${bloqueos.map(b => `<tr>
      <td>${b.tipo === "usuario" ? esc(b.usuario || "usuario " + b.valor) : "IP " + esc(b.valor)}</td><td>${esc(b.motivo)}</td><td class="num">${fechaHora(b.hasta)}</td>
      <td><button class="btn btn-suave btn-sm" data-levantar="${b.id}">Levantar</button></td></tr>`).join("")}</tbody></table></div>` : `<p class="tenue">Nadie está bloqueado.</p>`}
    <h4 class="seg-titulo">Incidentes</h4>
    ${incidentes.length ? `<div class="tabla-scroll"><table><thead><tr><th>Cuándo</th><th>Qué</th><th>Usuario</th><th>IP</th><th>Dónde</th><th>Fragmento</th></tr></thead><tbody>${incidentes.map(i => `<tr>
      <td class="num">${fechaHora(i.creado_en)}</td><td>${esc(TIPOS[i.tipo] || i.tipo)}</td><td>${esc(i.usuario || "—")}${i.rol ? `<span class="sub">${esc(ROLES[i.rol] || i.rol)}</span>` : ""}</td>
      <td class="num">${esc(i.ip || "—")}</td><td>${esc(i.origen || "")}</td><td class="itp-detalle">${esc(i.detalle || "")}</td></tr>`).join("")}</tbody></table></div>` : `<p class="tenue">Sin incidentes.</p>`}`;
  $$("#seg-cuerpo [data-levantar]").forEach(b => b.addEventListener("click", async () => {
    try { await API.post(`/seguridad/bloqueos/${b.dataset.levantar}/levantar`); toast("Bloqueo levantado.", "ok"); segIncidentes(); } catch (x) { toast(x.message, "error"); }
  }));
}

async function segLLM() {
  const filas = await API.get("/agente/invocaciones?limite=80");
  $("#seg-cuerpo").innerHTML = `<p class="tenue">Cada vez que el asistente llama al modelo queda guardado <strong>exactamente</strong> lo que se envió. Sirve para comprobar que no salió ningún dato personal: el paciente va como seudónimo (PX-…) y el clínico como rol + número.</p>
    ${filas.length ? `<div class="tabla-scroll"><table><thead><tr><th>#</th><th>Cuándo</th><th>Usuario</th><th>Paciente (seudónimo)</th><th>Modelo</th><th>Mensajes</th><th></th></tr></thead><tbody>${filas.map(f => `<tr>
      <td class="num">${f.id}</td><td class="num">${fechaHora(f.creado_en)}</td><td>${esc(f.usuario || "—")}<span class="sub">${esc(ROLES[f.rol] || f.rol || "")}</span></td>
      <td><code>${esc(f.paciente_alias || "sin paciente")}</code></td><td>${esc(f.modelo || "")}</td><td class="num">${f.mensajes}</td>
      <td><button class="btn btn-suave btn-sm" data-payload="${f.id}">Ver lo enviado</button></td></tr>`).join("")}</tbody></table></div>` : `<div class="vacio">El asistente todavía no ha llamado al modelo.</div>`}`;
  $$("#seg-cuerpo [data-payload]").forEach(b => b.addEventListener("click", async () => {
    try {
      const r = await API.get(`/agente/invocaciones/${b.dataset.payload}/payload`);
      const m = r.payload.mensajes.map(x => `<div class="seg-msg"><span>${esc(x.rol)}${x.herramienta ? " · " + esc(x.herramienta) : ""}</span><pre>${esc(x.contenido || (x.llamadas ? JSON.stringify(x.llamadas) : ""))}</pre></div>`).join("");
      abrirFormulario({ titulo: `Invocación ${r.invocacion_id} · ${r.alias || "sin paciente"}`, textoBoton: "Cerrar", alGuardar: async () => {},
        campos: `<details><summary>Prompt de sistema (${r.payload.sistema.length} caracteres)</summary><pre class="seg-pre">${esc(r.payload.sistema)}</pre></details>${m}` });
    } catch (x) { toast(x.message, "error"); }
  }));
}

if (typeof SECCIONES !== "undefined" && typeof ICONOS !== "undefined") {
  ICONOS.escudo = '<path d="M12 3l8 3v6c0 4.5-3.2 8-8 9-4.8-1-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>';
  SECCIONES.seguridad = { titulo: "Seguridad", icono: "escudo", roles: ["admin"], pintar: vistaSeguridad, grupo: "Red y seguridad" };
}
