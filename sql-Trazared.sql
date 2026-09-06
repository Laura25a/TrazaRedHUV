
DROP TABLE IF EXISTS observaciones;
DROP TABLE IF EXISTS contactos_institucion;
DROP TABLE IF EXISTS solicitudes_traslado;
DROP TABLE IF EXISTS usuarios;
DROP TABLE IF EXISTS pacientes;

CREATE TABLE pacientes (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    documento TEXT NOT NULL UNIQUE,
    eps TEXT NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE usuarios (
    id SERIAL PRIMARY KEY,
    tipo TEXT NOT NULL CHECK (tipo IN ('medico', 'admin', 'paciente')),
    nombre TEXT NOT NULL,
    correo TEXT NOT NULL UNIQUE,
    telefono TEXT,
    password_hash TEXT NOT NULL,
    paciente_id INTEGER REFERENCES pacientes(id) ON DELETE SET NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE solicitudes_traslado (
    id SERIAL PRIMARY KEY,
    fecha_hora TIMESTAMP NOT NULL,
    hospital_solicitante TEXT NOT NULL,
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id) ON DELETE CASCADE,
    institucion_destino_definitiva TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('pendiente', 'resuelto', 'fallido')),
    activo BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE contactos_institucion (
    id SERIAL PRIMARY KEY,
    solicitud_id INTEGER NOT NULL REFERENCES solicitudes_traslado(id) ON DELETE CASCADE,
    institucion_contactada TEXT NOT NULL,
    fecha_hora_solicitud TIMESTAMP NOT NULL,
    fecha_hora_respuesta TIMESTAMP,
    respuesta TEXT NOT NULL DEFAULT 'sin_respuesta'
        CHECK (respuesta IN ('aceptado', 'rechazado', 'sin_respuesta'))
);

CREATE TABLE observaciones (
    id SERIAL PRIMARY KEY,
    solicitud_id INTEGER NOT NULL REFERENCES solicitudes_traslado(id) ON DELETE CASCADE,
    medico_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    motivo TEXT NOT NULL,
    estado_paciente TEXT NOT NULL,
    nivel_urgencia TEXT NOT NULL CHECK (nivel_urgencia IN ('leve', 'moderado', 'critico')),
    activo BOOLEAN NOT NULL DEFAULT TRUE
);
