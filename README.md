# TrazaRed HUV

Sistema de trazabilidad para la remisión y el traslado de pacientes del Hospital
Universitario del Valle "Evaristo García" (HUV), desarrollado para la asignatura de
Salud Digital — Corte 1.

**Equipo:** Luz Angela Carabali Mulato, Laura Daniela Astudillo Ortega, Nicolás Zapata
Obando, David Ortega Quintero.

## El problema

El HUV no cuenta con un proceso trazable ni verificable para gestionar la remisión y el
traslado de pacientes hacia y desde otras instituciones. La gestión se hace caso a caso,
sin un registro único que confirme qué instituciones fueron contactadas, si tenían
disponibilidad, o si existía convenio vigente — un vacío documentado en la Sentencia
T-573-23 de la Corte Constitucional.

TrazaRed HUV registra cada gestión de traslado de forma única y visible para todas las
partes: quién contactó, a quién, cuándo, con qué respuesta, y si existía convenio
vigente.

## Estructura del repositorio

```
proyecto 1/
├── db/
│   └── schema.sql              # Esquema completo de PostgreSQL
├── docker/
│   └── docker-compose.yml      # Servidor HAPI FHIR + su propio PostgreSQL
├── python/
│   ├── main.py                 # API (FastAPI): autenticación, roles, CRUD
│   └── fhir_sync.py            # Servicio de integración PostgreSQL → FHIR
├── notebooks/
│   └── proyecto_trazared_huv.ipynb   # Notebook guía paso a paso
├── levantar_demo.py                 # Arranque automático de toda la demo (Windows/macOS/Linux)
├── guion_demo.md                    # Guion del pitch (10 min) y la demo (7 min)
├── documentacion_mapeo_roles.md      # Doc. técnica: mapeo BD → FHIR y roles
├── pass.env.example                  # Plantilla de variables de entorno
└── README.md
```

`pass.env` (con las credenciales reales) **no está en el repositorio** — cada quien lo
crea localmente en la raíz del proyecto a partir de `pass.env.example`, con las
variables `PG_CONNECTION_STRING`, `MONGO_CONNECTION_STRING`, `SECRET_KEY` y
`FHIR_BASE_URL`.

## Modelo de datos

**PostgreSQL (Neon):**
- `pacientes` — nombre, documento (único), género, EPS
- `usuarios` — con rol (`admin`, `medico`, `eps`, `paciente`) y vínculos opcionales a
  paciente/EPS
- `remisiones` — entidad principal: paciente, instituciones de origen/destino, motivo,
  estado, convenio vigente, creado_por, activo (soft delete)
- `observaciones` — signos vitales / triage asociados a una remisión, con código LOINC
- `remisiones_historial` — copia del estado anterior de cada remisión (soft edit)
- `auditoria` — registro de quién hizo qué acción, sobre qué y cuándo

**MongoDB (Atlas):**
- `gestiones_contacto` — bitácora anidada de intentos de contacto por remisión

## Roles y permisos

| Rol | Acceso |
|---|---|
| **Admin** | Control total; único que restaura registros eliminados |
| **Médico** | Crea/edita/elimina (soft delete) solo lo que él mismo generó |
| **EPS** | Lectura completa de sus propios pacientes y del hospital |
| **Paciente** | Lectura reducida: solo a dónde y en cuánto tiempo será remitido |

## Interoperabilidad FHIR

`fhir_sync.py` sincroniza los datos de negocio con un servidor HAPI FHIR (R4):

| Tabla PostgreSQL | Recurso FHIR |
|---|---|
| `pacientes` | `Patient` |
| `remisiones` | `Encounter` |
| `observaciones` | `Observation` |

El detalle campo por campo del mapeo, la matriz de endpoints × roles y las desviaciones
frente al modelo de la Semana 3 están en [`documentacion_mapeo_roles.md`](documentacion_mapeo_roles.md).

## Cómo correr el proyecto localmente

**1. Clonar el repositorio y crear el entorno virtual**
```powershell
git clone https://github.com/Laura25a/TrazaRedHUV.git
cd TrazaRedHUV
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install psycopg2-binary pymongo python-dotenv fastapi uvicorn python-multipart "passlib[bcrypt]" "python-jose[cryptography]" requests
```

**2. Configurar las variables de entorno**

Copia `pass.env.example` como `pass.env` en la raíz del proyecto y pon tus credenciales
reales:

```
PG_CONNECTION_STRING=postgresql://usuario:clave@host/neondb?sslmode=require
MONGO_CONNECTION_STRING=mongodb+srv://usuario:clave@cluster/...
SECRET_KEY=una_clave_secreta_propia
FHIR_BASE_URL=http://localhost:8081/fhir
```

**3. Crear el esquema en PostgreSQL**

Ejecuta `db/schema.sql` contra tu base de Neon (ver el notebook, Sección 3, o cualquier
cliente SQL).

**4. Correr la API**
```powershell
cd python
uvicorn main:app --reload
```
Documentación interactiva en `http://localhost:8000/docs`.

**5. Levantar el servidor FHIR**
```powershell
cd docker
docker compose up -d
```
Servidor disponible en `http://localhost:8081/fhir` (el compose publica el 8080 del
contenedor en el puerto 8081 del host — usa 8081 en túneles y navegador).

**6. Notebook guía**

`notebooks/proyecto_trazared_huv.ipynb` recorre todo el proceso paso a paso: creación
del primer usuario Admin, pruebas de los endpoints, sincronización con FHIR, y
verificación de la disponibilidad en línea vía Cloudflare Tunnel.

## Disponibilidad en línea (Cloudflare Tunnel)

<!-- URLS-DEMO:ini -->
URLs públicas generadas el **2026-09-07 12:08** con `levantar_demo` (Quick Tunnel:
efímeras — cambian en cada arranque):

- **API (FastAPI):** https://practices-camp-pan-statutory.trycloudflare.com — `/docs` verificado
- **Servidor FHIR (HAPI):** https://upcoming-combine-rouge-words.trycloudflare.com — `/fhir/metadata` verificado
<!-- URLS-DEMO:fin -->

> **Nota sobre la web de HAPI:** su interfaz administrativa (swagger-ui de HAPI) solo
> funciona completa en `localhost:8081` — HAPI anuncia una URL base fija y, como los
> Quick Tunnels cambian de dominio en cada arranque, desde fuera esa página no logra
> cargar su definición (mixed content). Los **recursos REST funcionan perfectamente por
> cualquier túnel**: para explorar desde fuera abre las URLs directas, p. ej.
> `https://<túnel>/fhir/Patient/1000`, `.../fhir/Encounter/1001`, `.../fhir/Observation/1002`
> o búsquedas como `.../fhir/Patient?gender=female`.

Para (re)generarlas no hay que hacer nada manual: corre **`python levantar_demo.py`**
desde la raíz. El script despierta Neon y MongoDB, levanta HAPI y la API esperando a
que cada uno responda, abre los dos túneles, detecta las URLs automáticamente, las
verifica y actualiza esta misma sección del README (quedan también en `URLs_demo.txt`,
local). Con `python levantar_demo.py --stop` se detiene todo.

### Cómo levantar la demo en otra máquina (Windows incluido)

Requisitos: **Python 3.10+**, **Docker Desktop corriendo** y **`cloudflared` instalado**
(si no lo tienes, instala primero: `winget install --id Cloudflare.cloudflared` o descarga
[`cloudflared-windows-amd64.exe`](https://github.com/cloudflare/cloudflared/releases/latest)
y déjalo en el `PATH` o junto a `levantar_demo.py`):

```
git pull
# pass.env en la raíz con las 8 variables (las 4 de siempre + las 4 DEMO_*)
python -m venv venv ; .\venv\Scripts\Activate.ps1
pip install psycopg2-binary pymongo python-dotenv fastapi uvicorn requests
python levantar_demo.py          # arranca todo (Docker Desktop debe estar corriendo)
python levantar_demo.py --stop   # apaga todo
```

En macOS/Linux el mismo flujo con `python3 -m venv venv && source venv/bin/activate`.
El script imprime las URLs al final y las deja también en `URLs_demo.txt`.

## Credenciales de demostración (datos sintéticos)

Todas las bases manejan **datos sintéticos de prueba**. Los usuarios los crea la
sección 6b del notebook:

| Rol | Usuario | Clave (variable en `pass.env`) |
|---|---|---|
| Admin | `admin@trazared.huv` | `DEMO_ADMIN_PASSWORD` |
| Médico | `medico@huv.gov.co` | `DEMO_MEDICO_PASSWORD` |
| EPS (Coosalud) | `eps@coosalud.com` | `DEMO_EPS_PASSWORD` |
| Paciente (Ana Torres) | `paciente@correo.com` | `DEMO_PACIENTE_PASSWORD` |

Las contraseñas **no están en el repositorio**: cada integrante las pone en su
`pass.env` (ver `pass.env.example`) y el notebook (§6b) las lee de ahí para crear los
usuarios y hacer login. Los valores se comparten por el canal privado y **se rotan
antes de la sustentación**.

## Entregables del Corte 1

- [x] Modelo relacional multi-tabla (`db/schema.sql`)
- [x] Servidor y modelado FHIR R4 (`docker/docker-compose.yml`)
- [x] Servicio de integración BD → FHIR (`python/fhir_sync.py`)
- [x] Roles de usuario con JWT (`python/main.py`)
- [x] Soft delete, soft edit y restauración
- [x] Disponibilidad en línea vía Cloudflare Tunnel (URLs arriba; pendiente prueba desde datos móviles)
- [x] Guion de pitch y demo (`guion_demo.md`)
- [x] Documentación técnica: mapeo BD → FHIR y justificación de roles (`documentacion_mapeo_roles.md`)
