-- TrazaRed HUV — Esquema completo de PostgreSQL

DROP TABLE IF EXISTS auditoria;
DROP TABLE IF EXISTS remisiones_historial;
DROP TABLE IF EXISTS observaciones;
DROP TABLE IF EXISTS remisiones;
DROP TABLE IF EXISTS usuarios;
DROP TABLE IF EXISTS consultas;
DROP TABLE IF EXISTS pacientes;

CREATE TABLE pacientes (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    documento TEXT NOT NULL UNIQUE,
    genero TEXT NOT NULL,
    eps TEXT NOT NULL
);

CREATE TABLE usuarios (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    correo TEXT NOT NULL UNIQUE,
    contrasena_hash TEXT NOT NULL,
    rol TEXT NOT NULL CHECK (rol IN ('admin','medico','eps','paciente')),
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    paciente_id INTEGER REFERENCES pacientes(id),
    eps_nombre TEXT
);

CREATE TABLE remisiones (
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

CREATE TABLE observaciones (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER NOT NULL REFERENCES remisiones(id) ON DELETE RESTRICT,
    tipo TEXT NOT NULL,
    codigo_loinc TEXT,
    valor NUMERIC NOT NULL,
    unidad TEXT,
    fecha_observacion TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE remisiones_historial (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER NOT NULL REFERENCES remisiones(id),
    dato_anterior JSONB NOT NULL,
    modificado_por INTEGER REFERENCES usuarios(id),
    modificado_en TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE auditoria (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER REFERENCES usuarios(id),
    accion TEXT NOT NULL,
    tabla TEXT NOT NULL,
    registro_id INTEGER NOT NULL,
    fecha TIMESTAMP NOT NULL DEFAULT now()
);
