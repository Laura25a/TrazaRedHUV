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

CREATE TABLE IF NOT EXISTS auditoria_operaciones_mongo (
    operacion UUID PRIMARY KEY
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
    CHECK (rol IN ('admin','medico','especialista','paciente','contable','eps','director','biomedico'));

-- R13: consultas rápidas de la auditoría por recurso (p. ej. "todo lo del paciente 12")
CREATE INDEX IF NOT EXISTS idx_auditoria_recurso ON auditoria (tabla, registro_id);

-- CORTE 2 — R10/R12 (Persona 2): reportes de IA con aprobación clínica
CREATE TABLE IF NOT EXISTS reportes_ia (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER REFERENCES remisiones(id),
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id),
    creado_por INTEGER REFERENCES usuarios(id),
    modelo TEXT,
    probabilidad_urgente NUMERIC NOT NULL,
    prediccion TEXT NOT NULL,
    critico_segun_umbral BOOLEAN NOT NULL DEFAULT FALSE,
    payload_seudonimizado JSONB NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','aprobado','rechazado')),
    observacion_revision TEXT,
    revisado_por INTEGER REFERENCES usuarios(id),
    revisado_en TIMESTAMP,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
-- Bases creadas antes de esta tabla completa: CREATE TABLE IF NOT EXISTS no agrega
-- columnas a una tabla vieja, así que se agregan una por una (idempotente).
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS remision_id INTEGER REFERENCES remisiones(id);
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS creado_por INTEGER REFERENCES usuarios(id);
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS modelo TEXT;
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS critico_segun_umbral BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS estado TEXT NOT NULL DEFAULT 'pendiente';
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS observacion_revision TEXT;
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS revisado_por INTEGER REFERENCES usuarios(id);
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS revisado_en TIMESTAMP;
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS creado_en TIMESTAMP NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS idx_reportes_ia_paciente ON reportes_ia (paciente_id, estado);

CREATE TABLE IF NOT EXISTS revisiones_rag (
    id SERIAL PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id),
    creado_por INTEGER NOT NULL REFERENCES usuarios(id),
    creado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    respuesta TEXT NOT NULL,
    fuentes JSONB NOT NULL CHECK (jsonb_typeof(fuentes) = 'array' AND fuentes <> '[]'::jsonb),
    revision_corpus TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aprobado', 'rechazado')),
    revisado_por INTEGER REFERENCES usuarios(id),
    revisado_en TIMESTAMPTZ,
    motivo TEXT,
    CHECK ((estado = 'pendiente' AND revisado_por IS NULL AND revisado_en IS NULL)
        OR (estado <> 'pendiente' AND revisado_por IS NOT NULL AND revisado_en IS NOT NULL)),
    CHECK (estado <> 'rechazado' OR nullif(btrim(motivo), '') IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_revisiones_rag_paciente ON revisiones_rag (paciente_id, estado, id DESC);
-- ============================================================================
-- CORTE 2 — R18/R20: notificaciones y alertas críticas (Persona 4, sistema reactivo)
-- ============================================================================
-- La bandeja de cada usuario (R18) y las alertas críticas con reconocimiento
-- (R20). Igual que lo anterior: solo AGREGA, se puede correr las veces que sea.

-- Alertas críticas: nace "activa" cuando el modelo pasa el umbral (evento
-- "alerta_critica"), queda visible hasta que un clínico la "reconoce" con nota,
-- y si nadie la reconoce a tiempo se marca escalada y se avisa al admin.
CREATE TABLE IF NOT EXISTS alertas (
    id SERIAL PRIMARY KEY,
    paciente_id INTEGER REFERENCES pacientes(id),
    nivel TEXT NOT NULL DEFAULT 'critico',
    estado TEXT NOT NULL CHECK (estado IN ('activa','reconocida')),
    mensaje TEXT NOT NULL,
    reporte_id INTEGER,                              -- reporte de IA de la Persona 2 (sin FK: tabla aparte)
    responsable_id INTEGER REFERENCES usuarios(id),  -- clínico responsable; NULL = la ve todo el equipo clínico
    creada_en TIMESTAMP NOT NULL DEFAULT now(),
    reconocida_en TIMESTAMP,
    reconocida_por INTEGER REFERENCES usuarios(id),
    nota TEXT,
    escalada BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_alertas_estado ON alertas (estado, creada_en);

-- Bandeja de notificaciones (R18): una fila por destinatario por evento.
-- "datos" guarda los campos extra del hecho (p. ej. usuario_afectado).
CREATE TABLE IF NOT EXISTS notificaciones (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    tipo TEXT NOT NULL,
    mensaje TEXT NOT NULL,
    leida BOOLEAN NOT NULL DEFAULT FALSE,
    fecha TIMESTAMP NOT NULL DEFAULT now(),
    paciente_id INTEGER REFERENCES pacientes(id),
    reporte_id INTEGER,
    remision_id INTEGER REFERENCES remisiones(id),
    alerta_id INTEGER REFERENCES alertas(id),
    datos JSONB
);
CREATE INDEX IF NOT EXISTS idx_notificaciones_usuario ON notificaciones (usuario_id, fecha);

-- ============================================================================
-- REDISEÑO "LIBRO DE RADICACIÓN" — el sistema como registro de remisiones
-- ============================================================================
-- Igual que todo lo anterior: solo AGREGA y se puede correr las veces que sea.
--
-- radicada_en: fecha Y HORA en que se asentó la remisión en el libro (antes
-- solo había fecha_solicitud, sin hora). Las remisiones viejas quedan con la
-- fecha de solicitud a las 00:00; las nuevas, con la hora real (DEFAULT now()).
-- El número de radicado (REM-AAAA-NNNNNN) y el folio se calculan del id: no
-- se guardan, así nunca se desincronizan.
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS radicada_en TIMESTAMP;
UPDATE remisiones SET radicada_en = fecha_solicitud::timestamp WHERE radicada_en IS NULL;
ALTER TABLE remisiones ALTER COLUMN radicada_en SET DEFAULT (now() AT TIME ZONE 'America/Bogota');

-- cerrada_en: cuándo se cerró el asiento (trasladado o rechazada). Con esto el
-- libro sabe cuánto duró cada remisión y la contabilidad sabe cuándo facturar.
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS cerrada_en TIMESTAMP;
-- institución que finalmente aceptó (puede ser distinta a la destino inicial)
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS institucion_acepta TEXT;

-- Pronósticos de la IA sobre cada remisión. Es un registro más: cada vez que
-- se anota una gestión, la IA vuelve a pronosticar y queda escrito qué dijo y
-- cuándo, para poder ver al cierre si acertó. Nunca se edita ni se borra.
CREATE TABLE IF NOT EXISTS pronosticos_remision (
    id SERIAL PRIMARY KEY,
    remision_id INTEGER NOT NULL REFERENCES remisiones(id),
    probabilidad_efectiva NUMERIC NOT NULL,
    probabilidad_al_radicar NUMERIC,
    riesgo TEXT NOT NULL,
    momento TEXT NOT NULL,                 -- radicacion | gestion | resuelta
    contactos INTEGER NOT NULL DEFAULT 0,
    version_modelo TEXT,
    detalle JSONB,                         -- sugerencias, tiempo estimado, causas
    creado_por INTEGER REFERENCES usuarios(id),
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pronosticos_remision ON pronosticos_remision (remision_id, id);

-- ============================================================================
-- CORTE 3 — Semana 13: el agente propone, el clínico decide
-- ============================================================================
-- Cada acción de escritura que propone el agente (por ejemplo, una remisión a
-- otra institución) queda aquí con su decisión: aprobada o rechazada, por quién
-- y con qué motivo. Lo rechazado no se borra: es evidencia (Res. 1995/1999).
-- La conversación en sí (mensajes) vive en las tablas que crea LangGraph
-- (checkpoints) y nunca guarda nombre, documento ni teléfono: solo el seudónimo.
CREATE TABLE IF NOT EXISTS agente_propuestas (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id),
    conversacion TEXT NOT NULL,
    paciente_id INTEGER REFERENCES pacientes(id),
    herramienta TEXT NOT NULL,
    argumentos JSONB NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente','aprobada','rechazada')),
    motivo_decision TEXT,
    remision_id INTEGER REFERENCES remisiones(id),
    creada_en TIMESTAMP NOT NULL DEFAULT now(),
    decidida_en TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agente_propuestas_usuario ON agente_propuestas (usuario_id, estado);

-- ============================================================================
-- Recuperación de información: imágenes con borrado LÓGICO (Res. 1995/1999)
-- ============================================================================
-- Eliminar una imagen la oculta de la ficha; el archivo DICOM nunca se borra del
-- PACS. Restaurarla (admin o ingeniería biomédica) cierra el registro, que queda
-- como historia de quién la ocultó, quién la recuperó y por qué.
CREATE TABLE IF NOT EXISTS imagenes_eliminadas (
    id SERIAL PRIMARY KEY,
    instance_id TEXT NOT NULL,
    paciente_id INTEGER REFERENCES pacientes(id),
    descripcion TEXT,
    modalidad TEXT,
    motivo TEXT NOT NULL,
    eliminada_por INTEGER NOT NULL REFERENCES usuarios(id),
    eliminada_en TIMESTAMP NOT NULL DEFAULT now(),
    vigente BOOLEAN NOT NULL DEFAULT TRUE,          -- TRUE = sigue oculta
    restaurada_por INTEGER REFERENCES usuarios(id),
    restaurada_en TIMESTAMP,
    motivo_restauracion TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_imagen_eliminada_vigente ON imagenes_eliminadas (instance_id) WHERE vigente;

-- ============================================================================
-- PROYECTO FINAL — Seguridad con STRIDE (R25, R27, R28) · solo AGREGA
-- ============================================================================
-- R27 · Cifrado de campos: documento, teléfono, dirección y correo del paciente
-- se guardan cifrados con AES-256-GCM (python/cripto.py). En la columna queda
-- "v1.nonce.cifrado"; la llave vive en el .env. La API cifra lo que encuentre en
-- claro cada vez que arranca (migración idempotente).
--   documento_idx: índice ciego (HMAC del documento) para buscar y para que el
--                  documento siga siendo único sin tenerlo en claro.
--   id_federado:   identificador con el que otra institución nombró al paciente
--                  cuando llegó por interoperabilidad y aún no se ha conciliado.
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS documento_idx TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS direccion TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS correo TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS id_federado TEXT;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS por_conciliar BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pacientes ADD COLUMN IF NOT EXISTS conciliado_con INTEGER REFERENCES pacientes(id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pacientes_documento_idx ON pacientes (documento_idx);
-- El cifrado de un mismo documento es distinto cada vez: la unicidad pasa al índice ciego.
ALTER TABLE pacientes DROP CONSTRAINT IF EXISTS pacientes_documento_key;

-- R28 · Auditoría a prueba de manipulación: cada registro guarda la huella del
-- anterior y la suya (HMAC con llave del .env). GET /auditoria/verificar recorre
-- la cadena. La API sella los registros viejos al arrancar.
ALTER TABLE auditoria ADD COLUMN IF NOT EXISTS hash_anterior TEXT;
ALTER TABLE auditoria ADD COLUMN IF NOT EXISTS hash TEXT;

-- R25 · Incidentes de seguridad (intentos de inyección al agente, firmas inválidas,
-- ráfagas) con el usuario y la IP que permiten identificarlos, y bloqueos temporales.
CREATE TABLE IF NOT EXISTS incidentes_seguridad (
    id SERIAL PRIMARY KEY,
    tipo TEXT NOT NULL,                      -- inyeccion_directa | inyeccion_indirecta | firma_invalida | ...
    usuario_id INTEGER REFERENCES usuarios(id),
    ip TEXT,
    origen TEXT,                             -- agente | rag | voz | interop | webhook_fhir
    detalle TEXT,                            -- fragmento que disparó la regla (recortado)
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_incidentes_reciente ON incidentes_seguridad (creado_en);

CREATE TABLE IF NOT EXISTS bloqueos_temporales (
    id SERIAL PRIMARY KEY,
    tipo TEXT NOT NULL CHECK (tipo IN ('usuario','ip')),
    valor TEXT NOT NULL,                     -- id del usuario o la IP
    motivo TEXT NOT NULL,
    hasta TIMESTAMP NOT NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    levantado_por INTEGER REFERENCES usuarios(id),
    levantado_en TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bloqueos_vigentes ON bloqueos_temporales (tipo, valor, hasta);

-- R24 · Cada llamada del agente al LLM deja el payload seudonimizado que se envió.
CREATE TABLE IF NOT EXISTS agente_invocaciones (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER REFERENCES usuarios(id),
    rol TEXT,
    conversacion TEXT,
    paciente_alias TEXT,
    modelo TEXT,
    payload JSONB NOT NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agente_invocaciones_usuario ON agente_invocaciones (usuario_id, id);
CREATE INDEX IF NOT EXISTS idx_agente_invocaciones_retencion ON agente_invocaciones (creado_en)
    WHERE payload ? 'mensajes';

-- ============================================================================
-- PROYECTO FINAL — Interoperabilidad entre sistemas (R29–R32) · solo AGREGA
-- ============================================================================
-- Cada institución socia entra con credenciales de SISTEMA (no de una persona).
--   client_id / client_secret_hash : lo que ELLOS usan para pedirnos un token (Argon2id)
--   secreto_firma                  : con lo que ELLOS firman cada mensaje (cifrado)
--   url_base + *_saliente          : dónde viven ELLOS y las credenciales que NOS dieron (cifradas)
CREATE TABLE IF NOT EXISTS sistemas_socios (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    client_id TEXT NOT NULL UNIQUE,
    client_secret_hash TEXT NOT NULL,
    secreto_firma TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT 'system/Bundle.write system/Task.write system/Observation.write',
    url_base TEXT,
    cliente_saliente TEXT,
    secreto_saliente TEXT,
    firma_saliente TEXT,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    de_prueba BOOLEAN NOT NULL DEFAULT FALSE,
    creado_por INTEGER REFERENCES usuarios(id),
    creado_en TIMESTAMP NOT NULL DEFAULT now()
);

-- Acuerdo federado y formato de salida opcionales por socio.
ALTER TABLE sistemas_socios ADD COLUMN IF NOT EXISTS secreto_federado TEXT;
ALTER TABLE sistemas_socios ADD COLUMN IF NOT EXISTS perfil_saliente TEXT;

-- Remisiones que cruzan la frontera: de dónde vino o a quién se envió.
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS origen_externo BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS socio_id INTEGER REFERENCES sistemas_socios(id);
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS referencia_externa TEXT;   -- cómo la llama el otro sistema
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS estado_interop TEXT;       -- enviada | recibida | respondida

-- Bitácora de cada mensaje que entra o sale. (socio, dirección, mensaje_id) es
-- único: un mensaje repetido se rechaza con 409 y no se procesa dos veces.
CREATE TABLE IF NOT EXISTS interop_mensajes (
    id SERIAL PRIMARY KEY,
    socio_id INTEGER REFERENCES sistemas_socios(id),
    direccion TEXT NOT NULL CHECK (direccion IN ('entrante','saliente')),
    mensaje_id TEXT NOT NULL,
    tipo TEXT NOT NULL,                       -- Bundle | Task | Observation
    estado TEXT NOT NULL,                     -- aceptado | por_conciliar | enviado | error
    codigo_http INTEGER,
    remision_id INTEGER REFERENCES remisiones(id),
    paciente_id INTEGER REFERENCES pacientes(id),
    referencia_externa TEXT,
    detalle TEXT,
    recurso JSONB,                            -- lo recibido/enviado (ya minimizado: sin documento ni teléfono)
    creado_en TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (socio_id, direccion, mensaje_id)
);
CREATE INDEX IF NOT EXISTS idx_interop_mensajes_remision ON interop_mensajes (remision_id);

-- R32 · Consentimiento del paciente para compartir sus datos con otra institución
-- (Ley 1581/2012: los datos de salud son sensibles y su transferencia exige autorización).
CREATE TABLE IF NOT EXISTS consentimientos (
    id SERIAL PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id),
    socio_id INTEGER NOT NULL REFERENCES sistemas_socios(id),
    finalidad TEXT NOT NULL DEFAULT 'Remisión y continuidad de la atención',
    medio TEXT NOT NULL DEFAULT 'escrito' CHECK (medio IN ('escrito','verbal','digital')),
    otorgado_en TIMESTAMP NOT NULL DEFAULT now(),
    registrado_por INTEGER REFERENCES usuarios(id),
    vigente BOOLEAN NOT NULL DEFAULT TRUE,
    revocado_en TIMESTAMP,
    revocado_por INTEGER REFERENCES usuarios(id)
);
CREATE INDEX IF NOT EXISTS idx_consentimientos_paciente ON consentimientos (paciente_id, socio_id, vigente);

-- Integridad de ficha y radicación.
CREATE OR REPLACE FUNCTION clave_institucion(nombre TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE clave
        WHEN 'huv' THEN 'hospital universitario del valle'
        ELSE clave
    END
    FROM (SELECT translate(lower(btrim(regexp_replace(nombre, '[[:space:]]+', ' ', 'g'))),
                           'áéíóúüÁÉÍÓÚÜ', 'aeiouuaeiouu') AS clave) normalizada
$$;

CREATE TABLE IF NOT EXISTS instituciones (
    nombre TEXT PRIMARY KEY,
    clave TEXT GENERATED ALWAYS AS (clave_institucion(nombre)) STORED UNIQUE,
    CHECK (clave <> '')
);

INSERT INTO instituciones (nombre) VALUES
    ('Hospital Universitario San José (Popayán)'),
    ('Hospital Francisco de Paula Santander (Santander de Quilichao)'),
    ('Hospital Local de Villa Rica'),
    ('ESE Norte 2 (Caloto)'),
    ('Hospital Departamental Tomás Uribe Uribe (Tuluá)'),
    ('Hospital San Rafael (Buga)'),
    ('Hospital Raúl Orejuela Bueno (Palmira)'),
    ('Hospital Departamental de Buenaventura'),
    ('Hospital Universitario del Valle'),
    ('Fundación Valle del Lili'),
    ('Clínica Imbanaco'),
    ('Clínica de Occidente'),
    ('Clínica Nuestra Señora de los Remedios'),
    ('Clínica Farallones'),
    ('Hospital San Juan de Dios Cali'),
    ('Clínica Colombia Cali'),
    ('Clínica Versalles'),
    ('Clínica Sebastián de Belalcázar')
ON CONFLICT DO NOTHING;

INSERT INTO instituciones (nombre)
SELECT DISTINCT btrim(regexp_replace(nombre, '[[:space:]]+', ' ', 'g'))
FROM (
    SELECT institucion_origen AS nombre FROM remisiones
    UNION SELECT institucion_destino FROM remisiones
    UNION SELECT nombre FROM sistemas_socios
) nombres
WHERE clave_institucion(nombre) <> ''
ORDER BY 1
ON CONFLICT DO NOTHING;

-- Conservar registros legados sin institución hasta que se corrijan.
UPDATE remisiones r SET institucion_origen = i.nombre
FROM instituciones i WHERE clave_institucion(r.institucion_origen) = i.clave
AND r.institucion_origen <> i.nombre;
UPDATE remisiones r SET institucion_destino = i.nombre
FROM instituciones i WHERE clave_institucion(r.institucion_destino) = i.clave
AND r.institucion_destino <> i.nombre;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conname = 'fk_remision_institucion_origen'
                   AND conrelid = 'remisiones'::regclass) THEN
        ALTER TABLE remisiones ADD CONSTRAINT fk_remision_institucion_origen
            FOREIGN KEY (institucion_origen) REFERENCES instituciones(nombre) NOT VALID;
        ALTER TABLE remisiones ADD CONSTRAINT fk_remision_institucion_destino
            FOREIGN KEY (institucion_destino) REFERENCES instituciones(nombre) NOT VALID;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_remision_paciente_fecha ON remisiones (paciente_id, fecha_solicitud);
CREATE INDEX IF NOT EXISTS idx_remision_institucion_origen ON remisiones (institucion_origen);
CREATE INDEX IF NOT EXISTS idx_remision_institucion_destino ON remisiones (institucion_destino);

-- Los importadores y la interoperabilidad pueden inscribir instituciones de sus fuentes.
CREATE OR REPLACE FUNCTION registrar_institucion(valor TEXT) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE canonica TEXT;
BEGIN
    IF clave_institucion(valor) = '' THEN
        RAISE check_violation USING MESSAGE = 'La institución es obligatoria';
    END IF;
    SELECT nombre INTO canonica FROM instituciones WHERE clave = clave_institucion(valor);
    IF NOT FOUND THEN
        INSERT INTO instituciones (nombre)
        VALUES (btrim(regexp_replace(valor, '[[:space:]]+', ' ', 'g')))
        ON CONFLICT DO NOTHING;
        SELECT nombre INTO canonica FROM instituciones WHERE clave = clave_institucion(valor);
    END IF;
    RETURN canonica;
END $$;

CREATE OR REPLACE FUNCTION validar_fecha_remision() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE nacimiento DATE;
DECLARE hoy DATE := (now() AT TIME ZONE 'America/Bogota')::date;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.paciente_id <> OLD.paciente_id AND NOT (
            OLD.origen_externo AND EXISTS (
                SELECT FROM pacientes p WHERE p.id = OLD.paciente_id AND p.por_conciliar
                    AND p.conciliado_con = NEW.paciente_id
            )
        ) THEN
            RAISE check_violation USING MESSAGE = 'El paciente del radicado es inmutable';
        END IF;
        IF NEW.paciente_id IS NOT DISTINCT FROM OLD.paciente_id
           AND NEW.fecha_solicitud IS NOT DISTINCT FROM OLD.fecha_solicitud
           AND NEW.radicada_en IS NOT DISTINCT FROM OLD.radicada_en THEN
            RETURN NEW;
        END IF;
    END IF;
    SELECT fecha_nacimiento INTO nacimiento FROM pacientes WHERE id = NEW.paciente_id FOR SHARE;
    IF NEW.fecha_solicitud > LEAST(hoy, NEW.radicada_en::date) THEN
        RAISE check_violation USING MESSAGE = 'La solicitud no puede ser posterior a la radicación ni a hoy';
    END IF;
    IF NEW.fecha_solicitud < nacimiento THEN
        RAISE check_violation USING MESSAGE = 'La solicitud no puede ser anterior al nacimiento';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER validar_fecha_remision
BEFORE INSERT OR UPDATE OF fecha_solicitud, radicada_en, paciente_id ON remisiones
FOR EACH ROW EXECUTE FUNCTION validar_fecha_remision();

CREATE OR REPLACE FUNCTION validar_fecha_paciente() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' AND NEW.fecha_nacimiento IS NOT DISTINCT FROM OLD.fecha_nacimiento THEN
        RETURN NEW;
    END IF;
    IF NEW.fecha_nacimiento > (now() AT TIME ZONE 'America/Bogota')::date THEN
        RAISE check_violation USING MESSAGE = 'La fecha de nacimiento no puede ser futura';
    END IF;
    IF EXISTS (SELECT FROM remisiones WHERE paciente_id = NEW.id AND fecha_solicitud < NEW.fecha_nacimiento) THEN
        RAISE check_violation USING MESSAGE = 'El nacimiento no puede ser posterior a una solicitud del paciente';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER validar_fecha_paciente
BEFORE INSERT OR UPDATE OF fecha_nacimiento ON pacientes
FOR EACH ROW EXECUTE FUNCTION validar_fecha_paciente();

CREATE OR REPLACE FUNCTION validar_transicion_remision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.estado = OLD.estado THEN RETURN NEW; END IF;
    IF NOT OLD.activo OR NOT (
        (OLD.estado = 'pendiente' AND NEW.estado IN ('en_gestion', 'aceptada', 'completada', 'rechazada'))
        OR (OLD.estado = 'en_gestion' AND NEW.estado IN ('aceptada', 'completada', 'rechazada'))
        OR (OLD.estado = 'aceptada' AND NEW.estado IN ('completada', 'rechazada'))
    ) THEN
        RAISE check_violation USING MESSAGE = 'La transición de estado no está permitida';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER validar_transicion_remision
BEFORE UPDATE OF estado ON remisiones
FOR EACH ROW EXECUTE FUNCTION validar_transicion_remision();

-- La bitácora documental conserva el id del radicado aunque se anule.
CREATE OR REPLACE FUNCTION impedir_borrado_remision() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE check_violation USING MESSAGE = 'Anula el radicado para conservar su historial y bitácora';
END $$;

CREATE OR REPLACE TRIGGER impedir_borrado_remision
BEFORE DELETE ON remisiones
FOR EACH ROW EXECUTE FUNCTION impedir_borrado_remision();

-- FHIR: observaciones de otro sistema, con episodio explícito o en historia del paciente.
ALTER TABLE observaciones ALTER COLUMN remision_id DROP NOT NULL;
ALTER TABLE observaciones ADD COLUMN IF NOT EXISTS paciente_id INTEGER REFERENCES pacientes(id);
ALTER TABLE observaciones ADD COLUMN IF NOT EXISTS socio_id INTEGER REFERENCES sistemas_socios(id);
ALTER TABLE observaciones ADD COLUMN IF NOT EXISTS profesional_id INTEGER REFERENCES usuarios(id);
ALTER TABLE observaciones ADD COLUMN IF NOT EXISTS profesional_fhir TEXT;
ALTER TABLE observaciones ADD COLUMN IF NOT EXISTS identificador_fhir TEXT;
CREATE INDEX IF NOT EXISTS idx_observaciones_paciente ON observaciones(paciente_id) WHERE paciente_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_observaciones_socio_fhir
    ON observaciones(socio_id, identificador_fhir) WHERE identificador_fhir IS NOT NULL;
DO $$ BEGIN
    ALTER TABLE observaciones ADD CONSTRAINT observacion_un_contexto
        CHECK ((remision_id IS NOT NULL) <> (paciente_id IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS profesional_fhir TEXT;
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS encuentro_fhir TEXT;
ALTER TABLE remisiones ADD COLUMN IF NOT EXISTS atendida_por INTEGER REFERENCES usuarios(id);

-- Las escrituras clínicas encolan la réplica en la misma transacción.
CREATE TABLE IF NOT EXISTS fhir_pendientes (
    tipo TEXT NOT NULL CHECK (tipo IN ('Patient', 'Encounter', 'Observation')),
    registro_id INTEGER NOT NULL,
    intentos INTEGER NOT NULL DEFAULT 0,
    intentar_en TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tipo, registro_id)
);

CREATE OR REPLACE FUNCTION encolar_replica_fhir() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO fhir_pendientes(tipo, registro_id) VALUES (TG_ARGV[0], NEW.id)
    ON CONFLICT (tipo, registro_id) DO UPDATE SET intentos = 0, intentar_en = now();
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS fhir_paciente ON pacientes;
CREATE TRIGGER fhir_paciente AFTER INSERT OR UPDATE OF nombre, documento, genero, fecha_nacimiento ON pacientes
    FOR EACH ROW WHEN (NOT NEW.por_conciliar AND NEW.conciliado_con IS NULL)
    EXECUTE FUNCTION encolar_replica_fhir('Patient');
DROP TRIGGER IF EXISTS fhir_remision ON remisiones;
CREATE TRIGGER fhir_remision AFTER INSERT OR UPDATE OF paciente_id, fecha_solicitud, motivo, estado, activo, atendida_por ON remisiones
    FOR EACH ROW EXECUTE FUNCTION encolar_replica_fhir('Encounter');
DROP TRIGGER IF EXISTS fhir_observacion ON observaciones;
CREATE TRIGGER fhir_observacion AFTER INSERT OR UPDATE OF remision_id, paciente_id, tipo, codigo_loinc, valor, unidad,
    fecha_observacion, profesional_id, profesional_fhir ON observaciones
    FOR EACH ROW EXECUTE FUNCTION encolar_replica_fhir('Observation');

-- Reactividad clínica. Cada toma mueve la revisión de su episodio y habilita
-- un nuevo intento. Se elimina la tarea al guardar el borrador o si faltan datos.
ALTER TABLE reportes_ia ADD COLUMN IF NOT EXISTS huella_automatica TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_reportes_ia_toma
    ON reportes_ia (remision_id, huella_automatica) WHERE huella_automatica IS NOT NULL;

CREATE TABLE IF NOT EXISTS analisis_automaticos_pendientes (
    remision_id INTEGER PRIMARY KEY REFERENCES remisiones(id),
    observacion_id INTEGER NOT NULL REFERENCES observaciones(id),
    intentar_en TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_notificaciones_cursor ON notificaciones (usuario_id, id);
CREATE TABLE IF NOT EXISTS fhir_observaciones_procesadas (
    fhir_id TEXT PRIMARY KEY,
    paciente_id INTEGER,
    valor NUMERIC,
    procesado_en TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS facturas_ia_pendientes (
    reporte_id INTEGER PRIMARY KEY REFERENCES reportes_ia(id)
);
