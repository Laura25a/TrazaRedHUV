"""
cripto.py — Las primitivas de seguridad del proyecto final (R27, R28 y la clase 14).

No importa nada del proyecto (ni auth ni eventos): por eso lo puede usar cualquier
módulo sin importaciones circulares. Cuatro cosas:

  1. CIFRADO DE CAMPOS (R27) · AES-256-GCM. Documento, teléfono, dirección y correo
     del paciente se guardan cifrados en PostgreSQL. Lo que queda en la columna es
     "versión.nonce.cifrado"; la llave vive en una variable de entorno (.env), nunca
     en el repositorio ni en la base. Cada valor va atado a SU paciente y SU campo
     (dato asociado, AAD): el cifrado del paciente 17 pegado en la fila del 18 no
     descifra. Si alguien cambia un solo carácter, GCM lo detecta y falla.
  2. ÍNDICE CIEGO · el cifrado cambia cada vez (nonce aleatorio), así que no sirve
     para WHERE. Para buscar por documento se guarda aparte HMAC(llave_indice, doc):
     siempre da lo mismo para el mismo documento y no se puede devolver al documento.
  3. SELLO DE AUDITORÍA (R28) · cada registro guarda la huella del anterior. La
     huella es un HMAC con llave: quien copie o edite la base sin el .env no puede
     recalcular la cadena para tapar un cambio.
  4. FIRMAS Y SECRETOS de interoperabilidad (R29, R31) · HMAC-SHA256 de mensajes,
     identificador federado del paciente y generación de secretos.

LLAVES. Cada una sale de su variable de entorno; si no está, se deriva de SECRET_KEY
(que ya es obligatoria) con una etiqueta distinta por uso, para que el sistema
arranque igual en el equipo de cualquier integrante. En producción se definen las
variables propias (ver .env.example): así rotar el secreto del JWT no deja ilegibles
los datos cifrados.

ROTACIÓN ("borrar el secreto no basta: hay que rotarlo"). Se agrega LLAVE_CIFRADO_V2
al .env; lo nuevo se cifra con v2, lo viejo se sigue leyendo con v1 y
POST /seguridad/cifrado/rotar vuelve a cifrar todo con la versión vigente.
"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import unicodedata

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CAMPOS_CIFRADOS = ("documento", "telefono", "direccion", "correo")
GENESIS = "0" * 64                       # huella "anterior" del primer registro de auditoría
SID_FEDERADO = "urn:salud-digital:sid-federado"   # system del identificador federado (perfil del curso)
_PATRON_CIFRADO = re.compile(r"^v(\d+)\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


# ── Llaves ────────────────────────────────────────────────────────────────
def _semilla() -> bytes:
    llave = os.getenv("SECRET_KEY")
    if not llave:
        raise RuntimeError("Falta SECRET_KEY: sin ella no se pueden derivar las llaves de cifrado")
    return llave.encode()


def _llave(variable: str, etiqueta: str) -> bytes:
    """32 bytes: de la variable de entorno (base64 url-safe o hex) o derivada de SECRET_KEY."""
    valor = (os.getenv(variable) or "").strip()
    if valor:
        try:
            crudo = bytes.fromhex(valor) if re.fullmatch(r"[0-9a-fA-F]{64}", valor) \
                else base64.urlsafe_b64decode(valor + "=" * (-len(valor) % 4))
        except Exception:
            crudo = b""
        if len(crudo) == 32:
            return crudo
        return hashlib.sha256(valor.encode()).digest()      # cualquier texto largo sirve de llave
    return hmac.new(_semilla(), f"trazared:{etiqueta}".encode(), hashlib.sha256).digest()


def _llaves_cifrado() -> dict:
    """{versión: llave}. v1 = LLAVE_CIFRADO (o derivada); v2, v3... = LLAVE_CIFRADO_V2..."""
    llaves = {1: _llave("LLAVE_CIFRADO", "cifrado-campos-v1")}
    for v in range(2, 10):
        if os.getenv(f"LLAVE_CIFRADO_V{v}"):
            llaves[v] = _llave(f"LLAVE_CIFRADO_V{v}", f"cifrado-campos-v{v}")
    return llaves


def version_vigente() -> int:
    return max(_llaves_cifrado())


def origen_llaves() -> dict:
    """Para el panel de seguridad: de dónde sale cada llave (nunca la llave)."""
    def de(variable):
        return "variable propia" if os.getenv(variable) else "derivada de SECRET_KEY"
    return {"cifrado": de("LLAVE_CIFRADO"), "indice": de("LLAVE_INDICE"),
            "auditoria": de("LLAVE_AUDITORIA"), "version_vigente": version_vigente()}


# ── 1. Cifrado de campos (AES-256-GCM) ────────────────────────────────────
def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _desb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def esta_cifrado(valor) -> bool:
    return isinstance(valor, str) and bool(_PATRON_CIFRADO.match(valor))


def version_de(valor) -> int | None:
    m = _PATRON_CIFRADO.match(valor) if isinstance(valor, str) else None
    return int(m.group(1)) if m else None


def cifrar(texto, aad: str) -> str | None:
    """texto -> "v1.nonce.cifrado". aad ata el valor a su fila y campo."""
    if texto is None or texto == "":
        return texto
    v = version_vigente()
    nonce = os.urandom(12)                                   # único por cifrado: nunca se repite
    cifrado = AESGCM(_llaves_cifrado()[v]).encrypt(nonce, str(texto).encode(), aad.encode())
    return f"v{v}.{_b64(nonce)}.{_b64(cifrado)}"


def descifrar(valor, aad: str):
    """Devuelve el texto. Un valor que no está cifrado (dato viejo) se devuelve igual.
    Si el valor fue alterado o pertenece a otra fila, lanza ValueError."""
    if not esta_cifrado(valor):
        return valor
    version, nonce, cifrado = valor.split(".")
    llave = _llaves_cifrado().get(int(version[1:]))
    if llave is None:
        raise ValueError(f"No hay llave para la versión {version}")
    try:
        return AESGCM(llave).decrypt(_desb64(nonce), _desb64(cifrado), aad.encode()).decode()
    except InvalidTag:
        raise ValueError("El dato cifrado fue alterado o no pertenece a este registro")


def aad_paciente(paciente_id: int, campo: str) -> str:
    return f"pacientes:{int(paciente_id)}:{campo}"


def descifrar_paciente(fila, id_campo: str = "id", prefijo: str = ""):
    """Descifra en una fila (dict) los campos personales del paciente.
    prefijo="paciente_" sirve para las consultas que traen paciente_documento, etc.
    Un valor ilegible no tumba la respuesta: sale como "[ilegible]" y se avisa en el log."""
    if fila is None:
        return None
    pid = fila.get(id_campo)
    for campo in CAMPOS_CIFRADOS:
        clave = prefijo + campo
        if clave in fila and esta_cifrado(fila[clave]) and pid is not None:
            try:
                fila[clave] = descifrar(fila[clave], aad_paciente(pid, campo))
            except ValueError as e:
                print(f"[cifrado] paciente {pid}, campo {campo}: {e}")
                fila[clave] = "[ilegible]"
    return fila


def descifrar_pacientes(filas, id_campo: str = "id", prefijo: str = ""):
    return [descifrar_paciente(f, id_campo, prefijo) for f in filas]


# ── 2. Índice ciego (buscar sin descifrar) ────────────────────────────────
def normalizar_documento(documento) -> str:
    """"1.144.123-456 " -> "1144123456": puntos, espacios y guiones no cambian el documento."""
    return re.sub(r"[\s.\-]", "", str(documento or "")).upper()


def indice_ciego(documento) -> str | None:
    normal = normalizar_documento(documento)
    if not normal:
        return None
    return hmac.new(_llave("LLAVE_INDICE", "indice-ciego-v1"), normal.encode(), hashlib.sha256).hexdigest()


def cifrar_fila_paciente(cur, paciente_id: int, datos: dict) -> None:
    """Deja cifrados (y con su índice ciego) los campos personales de UN paciente.
    Se llama dentro de la misma transacción que lo creó: el texto en claro nunca se confirma."""
    cambios, valores = [], []
    for campo in CAMPOS_CIFRADOS:
        if campo in datos and datos[campo] not in (None, "") and not esta_cifrado(datos[campo]):
            cambios.append(f"{campo} = %s")
            valores.append(cifrar(datos[campo], aad_paciente(paciente_id, campo)))
    if datos.get("documento") and not esta_cifrado(datos["documento"]):
        cambios.append("documento_idx = %s")
        valores.append(indice_ciego(datos["documento"]))
    if cambios:
        cur.execute(f"UPDATE pacientes SET {', '.join(cambios)} WHERE id = %s;", (*valores, paciente_id))


def cifrar_pendientes(conn) -> dict:
    """Migración idempotente: cifra lo que aún esté en claro y vuelve a cifrar lo que
    esté en una versión de llave anterior. Se corre al arrancar la API (auth.migrar_base)
    y al terminar de cargar el dataset. No confirma: lo hace quien llama."""
    vigente = version_vigente()
    cur = conn.cursor()
    cur.execute("SELECT id, documento, telefono, direccion, correo, documento_idx FROM pacientes ORDER BY id;")
    cifrados = recifrados = 0
    for pid, documento, telefono, direccion, correo, idx in cur.fetchall():
        actual = {"documento": documento, "telefono": telefono, "direccion": direccion, "correo": correo}
        cambios, valores, toca_claro, toca_version = [], [], False, False
        claro = {}
        for campo, valor in actual.items():
            if valor in (None, ""):
                continue
            if not esta_cifrado(valor):
                claro[campo] = valor
                toca_claro = True
            elif version_de(valor) != vigente:
                claro[campo] = descifrar(valor, aad_paciente(pid, campo))
                toca_version = True
            else:
                continue
            nuevo = cifrar(claro[campo], aad_paciente(pid, campo))
            # Se comprueba ANTES de escribir que lo cifrado vuelve a dar el original
            if descifrar(nuevo, aad_paciente(pid, campo)) != str(claro[campo]):
                raise RuntimeError(f"El cifrado del paciente {pid} ({campo}) no se pudo verificar")
            cambios.append(f"{campo} = %s")
            valores.append(nuevo)
        if idx is None and documento:
            doc_claro = claro.get("documento") or descifrar(documento, aad_paciente(pid, "documento"))
            cambios.append("documento_idx = %s")
            valores.append(indice_ciego(doc_claro))
        if cambios:
            cur.execute(f"UPDATE pacientes SET {', '.join(cambios)} WHERE id = %s;", (*valores, pid))
            cifrados += toca_claro
            recifrados += toca_version and not toca_claro
    cur.close()
    return {"cifrados": cifrados, "recifrados": recifrados, "version": vigente}


# ── 3. Sello de la auditoría encadenada (R28) ─────────────────────────────
def contenido_auditoria(reg_id, usuario_id, accion, tabla, registro_id, detalle, fecha_txt) -> str:
    """El texto exacto que se sella. Cualquier cambio en un campo cambia la huella."""
    partes = (reg_id, usuario_id, accion, tabla, registro_id, detalle, fecha_txt)
    return "\x1f".join("" if p is None else str(p) for p in partes)


def sellar(huella_anterior: str, contenido: str) -> str:
    """huella = HMAC(llave_auditoria, huella_anterior + contenido)."""
    llave = _llave("LLAVE_AUDITORIA", "auditoria-encadenada-v1")
    return hmac.new(llave, f"{huella_anterior}\x1e{contenido}".encode(), hashlib.sha256).hexdigest()


# ── 4. Firmas y secretos para interoperabilidad ───────────────────────────
def firmar(secreto: str, mensaje: bytes) -> str:
    return hmac.new(secreto.encode(), mensaje, hashlib.sha256).hexdigest()


def iguales(a: str, b: str) -> bool:
    """Comparación en tiempo constante (no revela cuántos caracteres coinciden)."""
    return hmac.compare_digest(str(a or "").encode(), str(b or "").encode())


def secreto_nuevo(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def id_federado(documento, secreto: str, tipo: str = "CC") -> str:
    """Mismo paciente en dos instituciones, sin enviar el documento:
    HMAC(secreto acordado, "CC|1144123456")."""
    mensaje = f"{tipo.upper()}|{normalizar_documento(documento)}"
    return hmac.new(secreto.encode(), mensaje.encode(), hashlib.sha256).hexdigest()


def derivar(etiqueta: str) -> str:
    """Secreto estable derivado de SECRET_KEY para un uso concreto (p. ej. el webhook FHIR)."""
    return hmac.new(_semilla(), f"trazared:{etiqueta}".encode(), hashlib.sha256).hexdigest()


def sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn")
