-- =====================================================
-- Migración Persona 3 · Corte 2
-- Requisitos: R08 (remisión entre médicos), R11 (PDF)
-- Idempotente: se puede correr varias veces sin dañar nada
-- =====================================================

-- ── R08: Remisiones entre médicos (médico → especialista) ──
-- Diferente de la tabla `remisiones` del Corte 1 (que es traslado a instituciones).
CREATE TABLE IF NOT EXISTS remisiones_medicas (
    id              SERIAL PRIMARY KEY,
    paciente_id     INTEGER NOT NULL REFERENCES pacientes(id),
    medico_id       INTEGER NOT NULL REFERENCES usuarios(id),
    especialista_id INTEGER NOT NULL REFERENCES usuarios(id),
    motivo          TEXT NOT NULL,
    observaciones   TEXT,
    estado          VARCHAR(20) NOT NULL DEFAULT 'pendiente'
                    CHECK (estado IN ('pendiente', 'aceptada', 'rechazada', 'completada')),
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT now(),
    aceptada_en     TIMESTAMP WITH TIME ZONE
);

CREATE INDEX IF NOT EXISTS idx_remisiones_medicas_paciente
    ON remisiones_medicas(paciente_id);
CREATE INDEX IF NOT EXISTS idx_remisiones_medicas_especialista
    ON remisiones_medicas(especialista_id);
CREATE INDEX IF NOT EXISTS idx_remisiones_medicas_medico
    ON remisiones_medicas(medico_id);
CREATE INDEX IF NOT EXISTS idx_remisiones_medicas_estado
    ON remisiones_medicas(estado);