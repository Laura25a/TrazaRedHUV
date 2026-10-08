-- TrazaRed HUV — Esquema de PostgreSQL (IDEMPOTENTE)
--
-- Este archivo se puede ejecutar las veces que sea contra la MISMA base:
-- si las tablas ya existen, CREATE TABLE IF NOT EXISTS no hace nada y NO
-- borra datos. Cualquier integrante del equipo puede correrlo sin riesgo
-- de reiniciar lo que los demás ya crearon.
--
-- ¿RESET TOTAL de verdad? Solo en un momento acordado por el equipo (borra
-- pacientes, remisiones, usuarios y gestiones de TODOS): descomenta el
-- bloque DROP, córrelo, y vuelve a correr este archivo para recrear tablas.

-- DROP TABLE IF EXISTS auditoria;
-- DROP TABLE IF EXISTS remisiones_historial;
-- DROP TABLE IF EXISTS observaciones;
-- DROP TABLE IF EXISTS remisiones;
-- DROP TABLE IF EXISTS usuarios;
-- DROP TABLE IF EXISTS consultas;
-- DROP TABLE IF EXISTS pacientes;

CREATE TABLE IF NOT EXISTS pacientes (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    documento TEXT NOT NULL UNIQUE,
    genero TEXT NOT NULL,
    eps TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    correo TEXT NOT NULL UNIQUE,
    contrasena_hash TEXT NOT NULL,
    rol TEXT NOT NULL CHECK (rol IN ('admin','medico','especialista','paciente','contable','eps')),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    paciente_id INTEGER REFERENCES pacientes(id),
    eps_nombre TEXT
);

CREATE TABLE IF NOT EXISTS remisiones (
    id SERIAL PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id) ON DELETE RESTRICT,
    institucion_origen TEXT NOT NULL,
    institucion_destino TEXT NOT NULL,
    fecha_solicitud DATE NOT NULL,
    motivo TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('pendiente','en_gestion','aceptada','rechazada','completada')),
    convenio_vigente BOOLEAN NOT NULL DEFAULT FALSE,
    creado_por INTEGER REFERENCES usuarios(id),
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS observaciones (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER NOT NULL REFERENCES remisiones(id) ON DELETE RESTRICT,
    tipo TEXT NOT NULL,
    codigo_loinc TEXT,
    valor NUMERIC NOT NULL,
    unidad TEXT,
    fecha_observacion TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS remisiones_historial (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER NOT NULL REFERENCES remisiones(id),
    dato_anterior JSONB NOT NULL,
    modificado_por INTEGER REFERENCES usuarios(id),
    modificado_en TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auditoria (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER REFERENCES usuarios(id),
    accion TEXT NOT NULL,
    tabla TEXT NOT NULL,
    registro_id INTEGER NOT NULL,
    fecha TIMESTAMP NOT NULL DEFAULT now()
);

-- ============================================================================
-- SEMANA 8 — Bloqueo de usuario por intentos fallidos (solo AGREGA, no borra)
-- ============================================================================
-- Todo lo de abajo es idempotente: se puede correr contra la base de Neon que
-- ya tiene datos; ADD COLUMN IF NOT EXISTS no toca lo existente.

-- usuarios: contador de intentos fallidos seguidos y bandera de bloqueo.
-- Al 3er intento fallido la API pone bloqueado = TRUE; solo un admin lo
-- desbloquea (POST /usuarios/{id}/desbloquear), lo que reinicia el contador.
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS intentos_fallidos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS bloqueado BOOLEAN NOT NULL DEFAULT FALSE;

-- auditoria: los eventos de login (fallido, bloqueo, exitoso) no apuntan a un
-- registro de negocio, así que registro_id pasa a permitir NULL, y "detalle"
-- guarda el contexto (p. ej. el correo con el que se intentó entrar y cuántos
-- intentos le quedaban). Las filas viejas no cambian.
ALTER TABLE auditoria ALTER COLUMN registro_id DROP NOT NULL;
ALTER TABLE auditoria ADD COLUMN IF NOT EXISTS detalle TEXT;

-- ============================================================================
-- SEMANA 8 — Ficha del paciente más completa (solo AGREGA, todo opcional)
-- ============================================================================
-- Datos que muestra la ficha de la interfaz. Son opcionales (NULL) para no
-- romper los pacientes que ya existen; el dataset del proyecto final los llena.
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS fecha_nacimiento DATE;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS telefono TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS tipo_sangre TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS alergias TEXT;

-- Las imágenes médicas NO se guardan en PostgreSQL: viven en el PACS (Orthanc)
-- como DICOM, y se enlazan con el paciente por el tag PatientID = documento.

-- ============================================================================
-- CORTE 2 — R01: cinco roles (solo AGREGA roles, no quita ninguno)
-- ============================================================================
--   admin, medico, especialista, paciente, contable (+ eps, que viene del Corte 1)
-- El CHECK original solo admitía admin/medico/eps/paciente. Se reemplaza por
-- uno que también acepta especialista y contable. Es idempotente: si ya está
-- actualizado, se vuelve a crear igual. La API corre este archivo en cada
-- arranque (auth.migrar_base), así que una base vieja se actualiza sola.
ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_rol_check;
ALTER TABLE usuarios ADD CONSTRAINT usuarios_rol_check
    CHECK (rol IN ('admin','medico','especialista','paciente','contable','eps'));

-- R13: consultas rápidas de la auditoría por recurso (p. ej. "todo lo del paciente 12")
CREATE INDEX IF NOT EXISTS idx_auditoria_recurso ON auditoria (tabla, registro_id);
