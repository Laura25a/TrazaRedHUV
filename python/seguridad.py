"""
seguridad.py — Controles del modelo STRIDE que viven en la API (proyecto final).

  · Límite de tasa por IP (Denial of service): "diez por ventana, el resto espera".
  · Guardia del agente (R25, Elevation of privilege): detecta texto que intenta darle
    órdenes al agente, lo registra con el usuario y la IP, y bloquea temporalmente a
    quien insiste.
  · Secreto del webhook FHIR (R29, Spoofing/Tampering).
  · Panel del administrador: estado del cifrado, cadena de auditoría, incidentes,
    bloqueos y límites (GET /seguridad/estado).

Las primitivas (AES-GCM, índice ciego, sello) están en cripto.py; aquí va la política.
"""
import os
import re
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import cripto
from auth import (PG_CONNECTION_STRING, get_db, get_usuario_actual, registrar_auditoria, requiere_rol,
                  verificar_cadena_auditoria)

router = APIRouter(tags=["Seguridad (STRIDE)"])


# ── IP real del cliente ───────────────────────────────────────────────────
def ip_cliente(request: Request) -> str:
    """Detrás de Cloudflare y nginx la conexión siempre llega desde nginx: la IP real
    viene en X-Real-IP (nginx la toma de CF-Connecting-IP). Sin proxy, la del socket."""
    ip = request.headers.get("x-real-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (ip or (request.client.host if request.client else "desconocida"))[:60]


# ── Límite de tasa (ventana deslizante en memoria) ────────────────────────
# "Diez por ventana, el resto espera." Tres cubetas, cada una con su dueño:
#   acceso   /login y /oauth/token      → por IP      (frena la fuerza bruta: cuentan los intentos fallidos)
#   socio    /interop/*                 → por IP      (la puerta de otro sistema)
#   ia       agente, RAG y voz          → por USUARIO (cada llamada cuesta: LLM)
#   general  todo lo demás              → por IP      (nginx ya limita a 10 r/s)
# Las de IA van por usuario y no por IP porque en un hospital muchos médicos salen
# a internet por la misma IP: uno que abusa no debe dejar sin asistente a los demás.
VENTANA_S = float(os.getenv("LIMITE_VENTANA_S") or "10")
LIMITE_SENSIBLE = int(os.getenv("LIMITE_SENSIBLE") or "10")     # intentos fallidos de login o de token de sistema
LIMITE_SOCIO = int(os.getenv("LIMITE_SOCIO") or "30")           # mensajes del sistema socio
LIMITE_IA = int(os.getenv("LIMITE_IA") or "20")                 # preguntas al asistente, por usuario
LIMITE_GENERAL = int(os.getenv("LIMITE_GENERAL") or "300")
MAX_CUERPO_INTEROP = int(os.getenv("MAX_CUERPO_INTEROP") or str(512 * 1024))   # 512 KB por mensaje del socio
_CUBETAS = [
    ("acceso", re.compile(r"^/(login|oauth/token)$"), LIMITE_SENSIBLE),
    ("socio", re.compile(r"^/interop/(Bundle|Task|Observation)$"), LIMITE_SOCIO),
    ("ia", re.compile(r"^/(agente/(chat|decidir)|rag/preguntar|bitacora/voz/)"), LIMITE_IA),
]
_golpes: dict = defaultdict(deque)
_candado = threading.Lock()
_rechazos = {"total": 0}


def _permitir(clave: str, limite: int, anotar: bool = True) -> tuple[bool, int]:
    """¿Cabe otra petición en la ventana? Con anotar=False solo consulta (no cuenta)."""
    ahora = time.monotonic()
    with _candado:
        cola = _golpes[clave]
        while cola and ahora - cola[0] > VENTANA_S:
            cola.popleft()
        if len(cola) >= limite:
            return False, max(1, int(VENTANA_S - (ahora - cola[0])) + 1)
        if anotar:
            cola.append(ahora)
        if len(_golpes) > 5000:                      # no crecer sin fin con IPs que no volvieron
            for k in [k for k, v in _golpes.items() if not v or ahora - v[-1] > VENTANA_S]:
                _golpes.pop(k, None)
        return True, 0


def _anotar(clave: str) -> None:
    with _candado:
        _golpes[clave].append(time.monotonic())


def _usuario_del_token(request: Request) -> Optional[str]:
    """Id del usuario si el token es válido (se verifica la firma: no se confía en un id inventado)."""
    cabecera = request.headers.get("authorization") or ""
    if not cabecera.lower().startswith("bearer "):
        return None
    try:
        from jose import jwt
        import auth
        datos = jwt.decode(cabecera[7:].strip(), auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        return str(datos.get("usuario_id")) if datos.get("usuario_id") is not None else None
    except Exception:
        return None


async def limite_de_tasa(request: Request, call_next):
    """Middleware. Quien se pasa recibe 429 con Retry-After; nadie más se ve afectado."""
    ruta = request.url.path
    if request.method == "OPTIONS" or ruta.startswith(("/app", "/eventos/stream", "/docs", "/openapi")):
        return await call_next(request)
    ip = ip_cliente(request)
    nombre, limite, dueno = "general", LIMITE_GENERAL, ip
    for cubeta, patron, tope in _CUBETAS:
        if patron.match(ruta):
            nombre, limite = cubeta, tope
            if cubeta == "ia":
                dueno = _usuario_del_token(request) or ip
            break
    # En "acceso" solo cuentan los intentos FALLIDOS: entrar bien no es un ataque (y el
    # tester inicia sesión con todos los roles seguidos); adivinar claves sí lo es.
    solo_fallos = nombre == "acceso"
    clave = f"{nombre}:{dueno}"
    ok, espera = _permitir(clave, limite, anotar=not solo_fallos)
    if not ok:
        _rechazos["total"] += 1
        return JSONResponse(status_code=429, headers={"Retry-After": str(espera)},
                            content={"detail": f"Demasiadas peticiones. Espera {espera} s e intenta de nuevo."})
    if ruta.startswith(("/interop/", "/oauth/")):
        tam = request.headers.get("content-length")
        if tam and tam.isdigit() and int(tam) > MAX_CUERPO_INTEROP:
            return JSONResponse(status_code=413, content={"detail": "El mensaje supera el tamaño permitido"})
    respuesta = await call_next(request)
    if solo_fallos and respuesta.status_code >= 400:
        _anotar(clave)
    return respuesta


# ── Guardia del agente (R25) ──────────────────────────────────────────────
# Capa "detector" de la defensa por capas. NO es la que protege los datos (eso lo
# hacen las herramientas, que revisan el rol en el backend y tienen el paciente fijado
# por el sistema): sirve para NO pasarle el texto al modelo, dejar constancia de quién
# lo intentó y frenar a quien insiste.
PATRON_INYECCION = re.compile(
    r"(ignor[ae]|olvid[ae]|omit[ae]|desobedece|descart[ae]|salt[ae]te|anula)\s+(todas?\s+)?(tus|las|sus|mis|estas)?\s*"
    r"(instrucciones|reglas|indicaciones|restricciones|pol[ií]ticas|[oó]rdenes)(\s+(anteriores|previas|del sistema))?"
    r"|ignore\s+(all\s+)?(previous|prior|above|your)\s+(instructions|rules)"
    r"|(ahora\s+eres|eres\s+ahora|act[uú]a\s+como|comp[oó]rtate\s+como|you\s+are\s+now)\s+(el\s+|la\s+|un\s+|una\s+)?"
    r"(admin\w*|root|sistema|system|desarrollador|dan\b|otro\s+asistente)"
    # "todos los pacientes" sí; "los pacientes pendientes" es una pregunta legítima
    r"|(muestra|mu[eé]strame|revela|lista|l[ií]stame|dame|imprime|exporta|env[ií]a)\s+(a\s+)?tod[oa]s\s+(los\s+|las\s+)?"
    r"(pacientes|historias|registros|usuarios|datos)"
    r"|(muestra|mu[eé]strame|revela|dame|imprime|exporta)\s+(todas?\s+|todos?\s+)?(las\s+|los\s+|tus\s+)?"
    r"(contrase[nñ]as|claves|tokens|secretos|llaves|credenciales)"
    r"|(system|sistema)\s*prompt|prompt\s+(del\s+)?sistema|tus\s+instrucciones\s+(internas|ocultas|del\s+sistema)"
    r"|(modo|mode)\s+(desarrollador|developer|dios|god|sin\s+restricciones)|jailbreak"
    r"|llama\s+a\s+la\s+herramienta|ejecuta\s+la\s+herramienta|usa\s+la\s+herramienta\s+\w+\s+con"
    r"|remite\s+a\s+todos|env[ií]a\s+(la\s+historia|los\s+datos|todo)\s+a(l)?\s+(correo|\S+@\S+)"
    r"|importante\s+para\s+el\s+asistente|nota\s+para\s+(la\s+ia|el\s+modelo|el\s+asistente)",
    re.IGNORECASE)

GUARDIA_MAX = int(os.getenv("GUARDIA_MAX_INTENTOS") or "3")          # intentos antes del bloqueo
GUARDIA_VENTANA_MIN = int(os.getenv("GUARDIA_VENTANA_MIN") or "10")  # ... dentro de estos minutos
GUARDIA_BLOQUEO_MIN = int(os.getenv("GUARDIA_BLOQUEO_MIN") or "15")  # cuánto dura el bloqueo
RESPUESTA_GUARDIA = ("Ese mensaje contiene una instrucción que intenta cambiar mis reglas o pedir datos "
                     "que no corresponden. No la ejecuté. El intento quedó registrado en la auditoría.")


def detectar_inyeccion(texto: str) -> Optional[str]:
    """Devuelve el fragmento sospechoso (recortado) o None."""
    m = PATRON_INYECCION.search(cripto.sin_tildes(texto or "")) or PATRON_INYECCION.search(texto or "")
    return m.group(0)[:120] if m else None


def neutralizar(texto: str) -> tuple[str, bool]:
    """Para DATOS (notas, documentos, resultados de herramientas): tacha las órdenes
    escondidas antes de que lleguen al modelo. Devuelve (texto, hubo_algo)."""
    nuevo, n = PATRON_INYECCION.subn("[instrucción eliminada]", texto or "")
    return nuevo, n > 0


def bloqueo_vigente(db, usuario_id: Optional[int], ip: Optional[str]) -> Optional[dict]:
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT id, tipo, motivo, hasta FROM bloqueos_temporales
                   WHERE levantado_en IS NULL AND hasta > now()
                     AND ((tipo = 'usuario' AND valor = %s) OR (tipo = 'ip' AND valor = %s))
                   ORDER BY hasta DESC LIMIT 1;""", (str(usuario_id), ip or ""))
    fila = cur.fetchone()
    cur.close()
    return fila


def exigir_sin_bloqueo(db, usuario: dict, ip: str) -> None:
    b = bloqueo_vigente(db, usuario["id"], ip)
    if b:
        raise HTTPException(status_code=403, detail=(
            f"El asistente está bloqueado temporalmente para tu cuenta hasta las {b['hasta']:%H:%M} "
            f"por intentos repetidos de alterar sus instrucciones. Un administrador puede levantar el bloqueo."))


def registrar_incidente(db, tipo: str, usuario_id: Optional[int], ip: Optional[str], origen: str,
                        detalle: str, bloquear: bool = True) -> dict:
    """Guarda el incidente (quién, desde dónde, qué), lo audita y, si ya van
    GUARDIA_MAX en la ventana, bloquea temporalmente al usuario y a la IP."""
    cur = db.cursor()
    cur.execute("""INSERT INTO incidentes_seguridad (tipo, usuario_id, ip, origen, detalle)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id;""", (tipo, usuario_id, ip, origen, (detalle or "")[:300]))
    incidente_id = cur.fetchone()[0]
    # Cuentan los intentos de la ventana posteriores al último bloqueo levantado por un
    # admin: levantar un bloqueo es empezar de cero, no quedar a un intento del siguiente.
    cur.execute("""SELECT COUNT(*) FROM incidentes_seguridad
                   WHERE tipo = %s AND creado_en > now() - make_interval(mins => %s)
                     AND ((%s::int IS NOT NULL AND usuario_id = %s) OR (%s::text IS NOT NULL AND ip = %s))
                     AND creado_en > COALESCE((SELECT max(levantado_en) FROM bloqueos_temporales
                                                WHERE (tipo = 'usuario' AND valor = %s::text)
                                                   OR (tipo = 'ip' AND valor = %s::text)), '-infinity');""",
                (tipo, GUARDIA_VENTANA_MIN, usuario_id, usuario_id, ip, ip, usuario_id, ip))
    intentos = cur.fetchone()[0]
    bloqueado = False
    if bloquear and intentos >= GUARDIA_MAX:
        motivo = f"{intentos} intentos de {tipo} en {GUARDIA_VENTANA_MIN} min"
        for clase, valor in (("usuario", usuario_id), ("ip", ip)):
            if valor is not None and valor != "":
                cur.execute("""INSERT INTO bloqueos_temporales (tipo, valor, motivo, hasta)
                               VALUES (%s, %s, %s, now() + make_interval(mins => %s));""",
                            (clase, str(valor), motivo, GUARDIA_BLOQUEO_MIN))
        bloqueado = True
    cur.close()
    registrar_auditoria(db, usuario_id, "inyeccion_detectada" if tipo.startswith("inyeccion") else tipo,
                        "incidentes_seguridad", incidente_id,
                        f"origen={origen} ip={ip} intento={intentos}/{GUARDIA_MAX} "
                        f"{'BLOQUEO ' + str(GUARDIA_BLOQUEO_MIN) + ' min ' if bloqueado else ''}fragmento={detalle[:120]!r}")
    try:                                     # aviso en vivo al administrador (R19)
        from eventos import ids_con_rol, publicar
        publicar(ids_con_rol(db, "admin"), {
            "tipo": "incidente_seguridad", "incidente_id": incidente_id, "origen": origen,
            "mensaje": ({"inyeccion_directa": f"Intento de manipular al asistente ({origen}), intento {intentos} de {GUARDIA_MAX}",
                         "inyeccion_indirecta": f"Se tachó una orden escondida en los datos ({origen})",
                         "firma_invalida": f"Mensaje con firma inválida en la entrada del socio ({detalle[:60]})"}
                        .get(tipo, f"Incidente de seguridad: {tipo} ({origen})")
                        + (f": cuenta e IP bloqueadas {GUARDIA_BLOQUEO_MIN} min" if bloqueado else ""))})
    except Exception:
        pass
    return {"incidente_id": incidente_id, "intentos": intentos, "bloqueado": bloqueado}


def guardia(db, usuario: dict, ip: str, texto: str, origen: str) -> Optional[dict]:
    """Se llama ANTES de pasarle una pregunta al modelo. Lanza 403 si la cuenta ya
    está bloqueada. Si el texto trae una orden para el asistente, registra el incidente
    y devuelve el dict del incidente (quien llama responde RESPUESTA_GUARDIA y no
    invoca al modelo); si está limpio, devuelve None."""
    exigir_sin_bloqueo(db, usuario, ip)
    fragmento = detectar_inyeccion(texto)
    if not fragmento:
        return None
    incidente = registrar_incidente(db, "inyeccion_directa", usuario["id"], ip, origen, fragmento)
    if incidente["bloqueado"]:
        # El intento que completa GUARDIA_MAX ya recibe el 403 (contrato: «al tercer
        # intento: 403»). El incidente y el bloqueo quedaron guardados antes de responder.
        exigir_sin_bloqueo(db, usuario, ip)
    return incidente


def registrar_inyeccion_indirecta(usuario_id: Optional[int], origen: str, detalle: str) -> None:
    """La orden venía escondida en un DATO (una nota, un documento): no es culpa de
    quien pregunta, así que se registra sin bloquear a nadie."""
    try:
        conn = psycopg2.connect(PG_CONNECTION_STRING, connect_timeout=5)
        try:
            registrar_incidente(conn, "inyeccion_indirecta", usuario_id, None, origen, detalle, bloquear=False)
        finally:
            conn.close()
    except Exception as e:
        print(f"[seguridad] no se pudo registrar la inyección indirecta: {e}")


# ── Minimización de lo que escribe el usuario (R12, R24) ──────────────────
def sanear_para_llm(texto: str, paciente: Optional[dict] = None) -> str:
    """El clínico puede escribir "¿cómo va Ana Torres, cédula 1175373460?". Antes de
    que eso salga hacia el LLM se quitan nombre, documento, teléfono, correo y dirección:
    el modelo solo conoce al paciente por su seudónimo."""
    salida = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "[CORREO]", texto or "")
    salida = re.sub(r"\b(calle|cll?|carrera|cra|kr|avenida|av|diagonal|transversal)\.?\s*\d+\s*\w?\s*(#|n[o°.]*)\s*\d+\w?\s*-?\s*\d*",
                    "[DIRECCIÓN]", salida, flags=re.IGNORECASE)
    salida = re.sub(r"\b\d[\d .-]{5,13}\d\b",
                    lambda m: ("[TELÉFONO]" if len(re.sub(r"\D", "", m.group())) == 10
                               and re.sub(r"\D", "", m.group()).startswith("3") else "[DOCUMENTO]")
                    if len(re.sub(r"\D", "", m.group())) >= 7 else m.group(), salida)
    if paciente:
        for campo in ("documento", "telefono", "correo", "direccion"):
            valor = str(paciente.get(campo) or "").strip()
            if valor and valor != "[ilegible]":
                salida = re.sub(rf"(?<!\w){re.escape(valor)}(?!\w)", "[DATO PERSONAL]", salida,
                                flags=re.IGNORECASE)
        partes = [p for p in str(paciente.get("nombre") or "").split() if len(p) >= 3]
        for frase in [" ".join(partes), " ".join(partes[:2])] + partes:
            if frase:
                salida = re.sub(rf"\b{re.escape(frase)}\b", "[PACIENTE]", salida, flags=re.IGNORECASE)
        salida = re.sub(r"(\[PACIENTE\]\s*){2,}", "[PACIENTE] ", salida)
    return salida


# ── Secreto del webhook FHIR (R29) ────────────────────────────────────────
CABECERA_WEBHOOK = "X-Webhook-Secret"


def secreto_webhook() -> str:
    """El que HAPI FHIR envía en cada aviso de la Subscription (channel.header). Sale
    de FHIR_WEBHOOK_SECRET o, si no está, se deriva de SECRET_KEY: nunca va en el repo."""
    return os.getenv("FHIR_WEBHOOK_SECRET") or cripto.derivar("webhook-fhir")


def exigir_webhook(request: Request) -> None:
    recibido = request.headers.get(CABECERA_WEBHOOK)
    if not recibido:
        raise HTTPException(status_code=401, detail=f"Falta la cabecera {CABECERA_WEBHOOK}")
    if not cripto.iguales(recibido, secreto_webhook()):
        raise HTTPException(status_code=403, detail="Secreto del webhook inválido")


# ── Panel del administrador ───────────────────────────────────────────────
@router.get("/seguridad/estado", dependencies=[Depends(requiere_rol("admin"))])
def estado_seguridad(db=Depends(get_db)):
    """Todo lo que el admin necesita ver de un vistazo: cifrado en reposo, cadena de
    auditoría, contraseñas, incidentes, bloqueos y límites. Nunca devuelve una llave."""
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE documento ~ '^v[0-9]+[.]') AS documento_cifrado,
                          COUNT(*) FILTER (WHERE telefono IS NOT NULL AND telefono <> '') AS con_telefono,
                          COUNT(*) FILTER (WHERE telefono ~ '^v[0-9]+[.]') AS telefono_cifrado,
                          COUNT(*) FILTER (WHERE documento_idx IS NOT NULL) AS con_indice,
                          COUNT(*) FILTER (WHERE por_conciliar) AS por_conciliar
                   FROM pacientes;""")
    cifrado = cur.fetchone()
    cur.execute("SELECT documento FROM pacientes ORDER BY id LIMIT 1;")
    muestra = cur.fetchone()
    cur.execute("""SELECT COUNT(*) FILTER (WHERE contrasena_hash LIKE '$argon2id$%%') AS argon2id,
                          COUNT(*) FILTER (WHERE contrasena_hash NOT LIKE '$argon2id$%%') AS por_migrar
                   FROM usuarios;""")
    claves = cur.fetchone()
    cur.execute("""SELECT COUNT(*) FILTER (WHERE creado_en > now() - interval '24 hours') AS ultimas_24h,
                          COUNT(*) AS total FROM incidentes_seguridad;""")
    incidentes = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS n FROM bloqueos_temporales WHERE levantado_en IS NULL AND hasta > now();")
    bloqueos = cur.fetchone()["n"]
    cur.close()
    return {
        "cifrado": {**cifrado, "algoritmo": "AES-256-GCM", "llaves": cripto.origen_llaves(),
                    "muestra_en_base": (muestra["documento"][:26] + "…") if muestra else None,
                    "todo_cifrado": cifrado["total"] == cifrado["documento_cifrado"]
                                    and cifrado["con_telefono"] == cifrado["telefono_cifrado"]},
        "auditoria": verificar_cadena_auditoria(db),
        "contrasenas": {**claves, "algoritmo": "Argon2id (bcrypt se migra al iniciar sesión)"},
        "incidentes": incidentes, "bloqueos_vigentes": bloqueos,
        "limites": {"ventana_s": VENTANA_S, "sensibles_por_ventana": LIMITE_SENSIBLE,
                    "socio_por_ventana": LIMITE_SOCIO, "ia_por_usuario": LIMITE_IA,
                    "general_por_ventana": LIMITE_GENERAL, "rechazadas_429": _rechazos["total"]},
        "guardia": {"intentos_para_bloqueo": GUARDIA_MAX, "ventana_min": GUARDIA_VENTANA_MIN,
                    "bloqueo_min": GUARDIA_BLOQUEO_MIN},
        "webhook_fhir": {"cabecera": CABECERA_WEBHOOK,
                         "secreto": "variable propia" if os.getenv("FHIR_WEBHOOK_SECRET") else "derivado de SECRET_KEY"},
    }


@router.get("/seguridad/incidentes", dependencies=[Depends(requiere_rol("admin"))])
def listar_incidentes(limite: int = Query(100, ge=1, le=500), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""SELECT i.id, i.tipo, i.origen, i.ip, i.detalle, i.usuario_id, u.correo AS usuario, u.rol,
                          i.creado_en AT TIME ZONE current_setting('TimeZone') AS creado_en
                   FROM incidentes_seguridad i LEFT JOIN usuarios u ON u.id = i.usuario_id
                   ORDER BY i.id DESC LIMIT %s;""", (limite,))
    filas = cur.fetchall()
    cur.close()
    return filas


@router.get("/seguridad/bloqueos", dependencies=[Depends(requiere_rol("admin"))])
def listar_bloqueos(todos: bool = Query(False), db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""SELECT b.id, b.tipo, b.valor, b.motivo, u.correo AS usuario,
                           b.hasta AT TIME ZONE current_setting('TimeZone') AS hasta,
                           b.creado_en AT TIME ZONE current_setting('TimeZone') AS creado_en,
                           (b.levantado_en IS NULL AND b.hasta > now()) AS vigente
                    FROM bloqueos_temporales b
                    LEFT JOIN usuarios u ON b.tipo = 'usuario' AND u.id::text = b.valor
                    {"" if todos else "WHERE b.levantado_en IS NULL AND b.hasta > now()"}
                    ORDER BY b.id DESC LIMIT 200;""")
    filas = cur.fetchall()
    cur.close()
    return filas


@router.post("/seguridad/bloqueos/{bloqueo_id}/levantar")
def levantar_bloqueo(bloqueo_id: int, db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    # Un incidente bloquea a la vez la cuenta y la IP (dos filas con la misma hora y
    # motivo). Levantar uno levanta también su pareja: si no, la cuenta seguía
    # bloqueada por la IP (o al revés) hasta que vencieran los 15 minutos.
    cur = db.cursor()
    cur.execute("""UPDATE bloqueos_temporales b SET levantado_por = %s, levantado_en = now()
                   FROM bloqueos_temporales o
                   WHERE o.id = %s AND b.levantado_en IS NULL
                     AND (b.id = o.id OR (b.creado_en = o.creado_en AND b.motivo = o.motivo))
                   RETURNING b.id, b.tipo, b.valor;""", (admin["id"], bloqueo_id))
    filas = cur.fetchall()
    cur.close()
    if not any(f[0] == bloqueo_id for f in filas):
        db.rollback()
        raise HTTPException(status_code=404, detail="Bloqueo no encontrado o ya levantado")
    for b_id, tipo, valor in filas:
        registrar_auditoria(db, admin["id"], "bloqueo_levantado", "bloqueos_temporales", b_id,
                            f"{tipo}={valor}", confirmar=False)
    db.commit()
    return {"levantado": True, "bloqueos": [{"id": f[0], "tipo": f[1]} for f in filas]}


@router.post("/seguridad/cifrado/rotar")
def rotar_llave(db=Depends(get_db), admin=Depends(requiere_rol("admin"))):
    """Vuelve a cifrar con la llave vigente lo que esté con una versión anterior
    (después de agregar LLAVE_CIFRADO_V2 al .env y reiniciar la API)."""
    r = cripto.cifrar_pendientes(db)
    registrar_auditoria(db, admin["id"], "llave_rotada", "pacientes", None,
                        f"recifrados={r['recifrados']} cifrados={r['cifrados']} version=v{r['version']}")
    return r


class TextoPrueba(BaseModel):
    texto: str = Field(min_length=1, max_length=500)


@router.post("/seguridad/guardia/probar", dependencies=[Depends(requiere_rol("admin"))])
def probar_guardia(body: TextoPrueba):
    """Para la sustentación: ¿el detector marcaría este texto? No registra ni bloquea."""
    fragmento = detectar_inyeccion(body.texto)
    return {"sospechoso": fragmento is not None, "fragmento": fragmento,
            "neutralizado": neutralizar(body.texto)[0]}
