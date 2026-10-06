// ==========================================================================
// TrazaRed HUV — conexión con la API (FastAPI)
// ==========================================================================
// La misma interfaz funciona en tres escenarios, y aquí se decide a dónde
// mandar cada petición:
//   1. Docker (nginx en http://localhost:8080): la API está detrás de /api y
//      HAPI detrás de /fhir (ver docker/nginx.conf). Mismo origen, sin CORS.
//   2. La API la sirve directamente en /app (uvicorn local o túnel): la API
//      está en el mismo origen, sin prefijo.
//   3. Abriendo index.html con doble clic (file://): se asume localhost:8000.

const API = (() => {
  const enApp = location.pathname.startsWith("/app");
  const archivo = location.protocol === "file:";
  const BASE = archivo ? "http://localhost:8000" : enApp ? "" : "/api";
  const FHIR = archivo || enApp ? "http://localhost:8081/fhir" : "/fhir";
  const CLAVE_TOKEN = "trazared_token";

  // El token se guarda en sessionStorage: se borra solo al cerrar la pestaña.
  function token() {
    try { return sessionStorage.getItem(CLAVE_TOKEN); } catch { return null; }
  }
  function guardarToken(t) {
    try { t ? sessionStorage.setItem(CLAVE_TOKEN, t) : sessionStorage.removeItem(CLAVE_TOKEN); } catch { /* sin storage */ }
  }

  // Lee el contenido del JWT (sin verificarlo: eso lo hace la API) para saber
  // cuándo vence y mostrar la cuenta regresiva.
  function datosToken() {
    const t = token();
    if (!t) return null;
    try {
      const b64 = t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      return JSON.parse(atob(b64));
    } catch { return null; }
  }

  class ErrorApi extends Error {
    constructor(status, detalle) {
      const msg = typeof detalle === "string" ? detalle
        : detalle && detalle.mensaje ? detalle.mensaje
        : Array.isArray(detalle) ? detalle.map(d => d.msg).join(" · ")
        : `Error ${status}`;
      super(msg);
      this.status = status;
      this.detalle = detalle;
    }
  }

  // Se llama cuando la API responde 401 con una sesión abierta (token vencido,
  // usuario bloqueado mientras tanto, etc.). app.js la reemplaza.
  let alExpirar = () => {};

  async function pedir(metodo, ruta, cuerpo, opciones = {}) {
    const headers = {};
    const t = token();
    if (t) headers.Authorization = `Bearer ${t}`;
    let body;
    if (cuerpo instanceof URLSearchParams || cuerpo instanceof FormData) {
      body = cuerpo;            // el navegador pone el Content-Type (form / multipart)
    } else if (cuerpo !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(cuerpo);
    }
    let res;
    try {
      res = await fetch(BASE + ruta, { method: metodo, headers, body });
    } catch {
      throw new ErrorApi(0, "No hay conexión con la API. ¿Está corriendo el contenedor api?");
    }
    if (res.status === 204) return null;
    if (res.ok && opciones.blob) return res.blob();   // imágenes del PACS
    let datos = null;
    try { datos = await res.json(); } catch { /* respuesta sin cuerpo */ }
    if (!res.ok) {
      if (res.status === 401 && t && !opciones.esLogin) {
        // La sesión ya no sirve: se vuelve al login y la vista que hizo esta
        // petición se queda esperando para siempre (ya no hay nada que pintar).
        alExpirar(datos && datos.detail);
        return new Promise(() => {});
      }
      throw new ErrorApi(res.status, datos ? datos.detail : null);
    }
    return datos;
  }

  return {
    BASE, FHIR,
    token, guardarToken, datosToken,
    set alExpirar(fn) { alExpirar = fn; },
    ErrorApi,

    // Login: FastAPI espera un formulario OAuth2 (username / password)
    async login(usuario, clave) {
      const form = new URLSearchParams({ username: usuario, password: clave });
      const datos = await pedir("POST", "/login", form, { esLogin: true });
      guardarToken(datos.access_token);
      return datos;
    },
    salir() { guardarToken(null); },

    get: (ruta) => pedir("GET", ruta),
    blob: (ruta) => pedir("GET", ruta, undefined, { blob: true }),
    subir: (ruta, formData) => pedir("POST", ruta, formData),
    post: (ruta, cuerpo) => pedir("POST", ruta, cuerpo),
    put: (ruta, cuerpo) => pedir("PUT", ruta, cuerpo),
    del: (ruta) => pedir("DELETE", ruta),

    // Indicadores de conexión: /health de la API y /metadata de HAPI FHIR
    async estadoServicios() {
      const estado = { api: "error", postgres: "error", mongo: "error", pacs: "error", fhir: "error" };
      try {
        const res = await fetch(BASE + "/health");
        if (res.ok) Object.assign(estado, await res.json());
      } catch { /* API caída */ }
      try {
        const mismoOrigen = FHIR.startsWith("/");
        const res = await fetch(FHIR + "/metadata?_summary=true",
          mismoOrigen ? {} : { mode: "no-cors" });
        estado.fhir = mismoOrigen ? (res.ok ? "ok" : "error") : "ok";
      } catch { /* HAPI caído */ }
      return estado;
    },
  };
})();
