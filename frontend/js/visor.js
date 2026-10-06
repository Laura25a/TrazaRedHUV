// ==========================================================================
// visor.js — visor de imágenes médicas: brillo, contraste, negativo, zoom y
// desplazamiento (misma idea del cuaderno de la semana 8).
//
// Para cada píxel de la imagen ORIGINAL se calcula:
//     salida = contraste × (entrada − 128) + 128 + brillo      (recortado a 0..255)
// El contraste "estira" los valores alrededor del gris medio (128) y el brillo
// los desplaza. Como solo hay 256 entradas posibles, la fórmula se calcula una
// vez por valor (tabla LUT) y cada píxel es una consulta a la tabla.
// ==========================================================================
"use strict";

const Visor = {
  dialogo: null, lienzo: null, ctx: null,
  original: null,     // píxeles tal como llegaron del PACS (nunca se modifican)
  buffer: null,       // canvas oculto, tamaño real de la imagen, con el resultado
  ancho: 0, alto: 0,
  brillo: 0, contraste: 1, invertir: false,
  zoom: 1, panX: 0, panY: 0,
  arrastre: null, pendiente: false,

  tablaLUT(brillo, contraste, invertir) {
    const t = new Uint8ClampedArray(256);
    for (let e = 0; e < 256; e++) {
      const s = Math.min(255, Math.max(0, contraste * (e - 128) + 128 + brillo));
      t[e] = invertir ? 255 - s : s;
    }
    return t;
  },

  init() {
    const $ = (s) => document.querySelector(s);
    this.dialogo = $("#visor");
    this.lienzo = $("#visor-canvas");
    this.ctx = this.lienzo.getContext("2d");
    const caja = $("#visor-lienzo");

    $("#visor-cerrar").addEventListener("click", () => this.dialogo.close());
    this.dialogo.addEventListener("click", (e) => { if (e.target === this.dialogo) this.dialogo.close(); });
    this.dialogo.addEventListener("close", () => { this.original = null; this.buffer = null; });

    $("#ctl-brillo").addEventListener("input", (e) => { this.brillo = +e.target.value; this.programar(); });
    $("#ctl-contraste").addEventListener("input", (e) => { this.contraste = +e.target.value; this.programar(); });
    $("#ctl-invertir").addEventListener("change", (e) => { this.invertir = e.target.checked; this.programar(); });
    $("#btn-zoom-mas").addEventListener("click", () => this.zoomEn(1.25, this.lienzo.width / 2, this.lienzo.height / 2));
    $("#btn-zoom-menos").addEventListener("click", () => this.zoomEn(0.8, this.lienzo.width / 2, this.lienzo.height / 2));
    $("#btn-ajustar").addEventListener("click", () => this.ajustar());
    $("#btn-restablecer").addEventListener("click", () => this.restablecer());

    this.lienzo.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = this.lienzo.getBoundingClientRect();
      const k = this.lienzo.width / r.width;
      this.zoomEn(e.deltaY < 0 ? 1.15 : 1 / 1.15, (e.clientX - r.left) * k, (e.clientY - r.top) * k);
    }, { passive: false });
    this.lienzo.addEventListener("pointerdown", (e) => {
      this.lienzo.setPointerCapture(e.pointerId);
      this.arrastre = { x: e.clientX, y: e.clientY };
      caja.classList.add("moviendo");
    });
    this.lienzo.addEventListener("pointermove", (e) => {
      if (this.arrastre) {
        const k = this.lienzo.width / this.lienzo.getBoundingClientRect().width;
        this.panX += (e.clientX - this.arrastre.x) * k;
        this.panY += (e.clientY - this.arrastre.y) * k;
        this.arrastre = { x: e.clientX, y: e.clientY };
        this.dibujar();
      }
      this.leerPixel(e);
    });
    const soltar = () => { this.arrastre = null; caja.classList.remove("moviendo"); };
    this.lienzo.addEventListener("pointerup", soltar);
    this.lienzo.addEventListener("pointercancel", soltar);
    this.lienzo.addEventListener("pointerleave", () => { document.querySelector("#visor-lectura").textContent = ""; });
    this.lienzo.addEventListener("dblclick", () => this.ajustar());
    window.addEventListener("resize", () => { if (this.dialogo.open) { this.medir(); this.dibujar(); } });
  },

  // Abre la imagen (URL blob: ya descargada con el token) en el visor
  async abrir({ url, titulo, subtitulo }) {
    document.querySelector("#visor-titulo").textContent = titulo || "Imagen";
    document.querySelector("#visor-sub").textContent = subtitulo || "";
    this.dialogo.showModal();
    const img = new Image();
    img.src = url;
    await img.decode();
    this.ancho = img.naturalWidth;
    this.alto = img.naturalHeight;
    const tmp = document.createElement("canvas");
    tmp.width = this.ancho; tmp.height = this.alto;
    const tctx = tmp.getContext("2d");
    tctx.drawImage(img, 0, 0);
    this.original = tctx.getImageData(0, 0, this.ancho, this.alto);
    this.buffer = tmp;
    this.medir();
    this.restablecer();
  },

  medir() {
    const r = this.lienzo.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.lienzo.width = Math.max(1, Math.round(r.width * dpr));
    this.lienzo.height = Math.max(1, Math.round(r.height * dpr));
  },

  restablecer() {
    this.brillo = 0; this.contraste = 1; this.invertir = false;
    document.querySelector("#ctl-brillo").value = 0;
    document.querySelector("#ctl-contraste").value = 1;
    document.querySelector("#ctl-invertir").checked = false;
    this.procesar();
    this.ajustar();
  },

  ajustar() {
    if (!this.original) return;
    this.zoom = Math.min(this.lienzo.width / this.ancho, this.lienzo.height / this.alto) * 0.95;
    this.panX = (this.lienzo.width - this.ancho * this.zoom) / 2;
    this.panY = (this.lienzo.height - this.alto * this.zoom) / 2;
    this.dibujar();
  },

  // Zoom manteniendo fijo el punto (px, py) del lienzo
  zoomEn(factor, px, py) {
    if (!this.original) return;
    const nuevo = Math.min(20, Math.max(0.05, this.zoom * factor));
    const k = nuevo / this.zoom;
    this.panX = px - (px - this.panX) * k;
    this.panY = py - (py - this.panY) * k;
    this.zoom = nuevo;
    this.dibujar();
  },

  // Agrupa varios cambios de los deslizadores en un solo cálculo por cuadro
  programar() {
    document.querySelector("#val-brillo").textContent = this.brillo;
    document.querySelector("#val-contraste").textContent = this.contraste.toFixed(2);
    if (this.pendiente) return;
    this.pendiente = true;
    requestAnimationFrame(() => { this.pendiente = false; this.procesar(); this.dibujar(); });
  },

  procesar() {
    if (!this.original) return;
    const lut = this.tablaLUT(this.brillo, this.contraste, this.invertir);
    const src = this.original.data;
    const res = new ImageData(this.ancho, this.alto);
    const dst = res.data;
    for (let i = 0; i < src.length; i += 4) {
      dst[i] = lut[src[i]]; dst[i + 1] = lut[src[i + 1]]; dst[i + 2] = lut[src[i + 2]]; dst[i + 3] = src[i + 3];
    }
    this.buffer.getContext("2d").putImageData(res, 0, 0);
    document.querySelector("#val-brillo").textContent = this.brillo;
    document.querySelector("#val-contraste").textContent = this.contraste.toFixed(2);
  },

  dibujar() {
    if (!this.buffer) return;
    const c = this.ctx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.fillStyle = "#000";
    c.fillRect(0, 0, this.lienzo.width, this.lienzo.height);
    c.imageSmoothingEnabled = this.zoom < 2;       // al acercar mucho se ven los píxeles reales
    c.setTransform(this.zoom, 0, 0, this.zoom, this.panX, this.panY);
    c.drawImage(this.buffer, 0, 0);
    document.querySelector("#val-zoom").textContent = `${Math.round(this.zoom * 100)}%`;
  },

  // Muestra coordenada y valor del píxel original bajo el puntero
  leerPixel(e) {
    if (!this.original) return;
    const r = this.lienzo.getBoundingClientRect();
    const k = this.lienzo.width / r.width;
    const x = Math.floor(((e.clientX - r.left) * k - this.panX) / this.zoom);
    const y = Math.floor(((e.clientY - r.top) * k - this.panY) / this.zoom);
    const salida = document.querySelector("#visor-lectura");
    if (x < 0 || y < 0 || x >= this.ancho || y >= this.alto) { salida.textContent = ""; return; }
    const v = this.original.data[(y * this.ancho + x) * 4];
    salida.textContent = `x ${x} · y ${y} · valor ${v} → ${this.tablaLUT(this.brillo, this.contraste, this.invertir)[v]}`;
  },
};

document.addEventListener("DOMContentLoaded", () => Visor.init());
