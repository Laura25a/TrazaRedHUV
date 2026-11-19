# Sustentación y evidencia entregable

## Estado y alcance

| Punto | Entrega verificable | Límite |
|---|---|---|
| L01 | README, arquitectura, roles, métricas, STRIDE y marco normativo versionados | Las métricas sintéticas y los documentos académicos conservan sus límites declarados |
| L02 | `tester.yaml` raíz, métodos, rutas, roles, respuestas e indicadores; ejecutor HTTP | Contar respuestas correctas no acredita por sí solo un requisito completo |
| L03 | Imágenes externas y base Python por digest, dependencias API/ML fijadas, Compose raíz y compatibilidad, verificación pública | Disponibilidad comprobada a una hora concreta; el equipo anfitrión y el túnel deben seguir encendidos |
| L04 | Escenarios HTTP de fechas, Mongo huérfano y conciliación 202; regresiones API y Chromium, JUnit y JSON | Las pruebas autenticadas usan bases efímeras. El tester completo público se identifica por separado cuando se ejecute |
| L05 / R33 | Pendiente por orden expresa del dueño | No implementar ni ejecutar hasta nueva orden; un socio sintético no es otra aplicación real |

## Verificación del 17 de noviembre de 2026

Resultado versionado: [evidencia/2026-11-17-sustentacion.json](evidencia/2026-11-17-sustentacion.json).
Incluye comandos, revisión base, huellas del árbol de trabajo y resultados sanitizados,
sin contraseñas ni cuerpos clínicos.

- `bash scripts/probar_integracion.sh /tmp/trazared-s12-venv/bin/python ../evidencia-trazared/sustentacion-2026-11-17`: salida 0; `50 passed, 11 warnings in 118.68s (0:01:58)`.
- Verificador público con `--navegador`: salida 0; 38 comprobaciones aprobadas, cero fallos y L05 pendiente. Seis entradas públicas, 26 recursos por entrada, incluidos controles móviles.
- API y ML construidos con los locks y `pip check` sin incompatibilidades; desplegados conservando volúmenes. Coinciden 40 archivos Python de API y 4 de ML, y 79 y 25 dependencias respectivamente.
- Tres comprobaciones sobre ML desplegado: inferencia sintética 200, presión incoherente 422 y consulta del modelo de remisiones 200. No se reentrenó ni guardó un reporte clínico.

Las primeras corridas simultáneas a la construcción de imágenes tuvieron tiempos
agotados en login y agente. La prueba de documentos ahora espera el HTTP real del login
antes de verificar la pantalla. La corrida final completa se ejecutó sin construir
imágenes en paralelo, sin aumentar el límite del agente ni desactivar pruebas.

Las capturas y JUnit están en `../evidencia-trazared/sustentacion-2026-11-17/`, fuera
del clon. No se ejecutó el tester completo autenticado contra la URL pública en esta tarea.

## Arranque y versión

Desde la raíz del clon `TrazaRedHUV`, con configuración privada entregada fuera de Git:

```sh
docker compose --env-file RUTA_PRIVADA.env --profile tunel-fijo up -d --build
docker compose ps
```

Las variables de interpolación y las de API deben estar configuradas según `.env.example`.
El túnel fijo necesita su token privado y el hostname configurado en Cloudflare.
La instalación pública existente usa además su override privado; conservar ese archivo
y el nombre de proyecto `trazared-publico` al actualizarla. No ejecutar un segundo
Compose con otro nombre contra los mismos puertos. No ejecutar `down -v`.

El archivo `docker/docker-compose.yml` incluye el de raíz y conserva los mismos volúmenes.
El Compose base también inicia el túnel rápido; al usar el fijo, detener el servicio
`cloudflared` con el mismo proyecto y configuración, manteniendo `cloudflared-fijo` activo.
Los digests fijan las imágenes externas ya usadas, sin cambiar versiones mayores de bases
ni migrar sus datos. `requirements-api.lock.txt` y `ml/requirements.lock.txt` restringen
las dependencias transitivas a las versiones inspeccionadas en los servicios.
Ambos builds ejecutan `pip check`. Al actualizar dependencias o imágenes, actualizar
también los locks y repetir la verificación; los digests no son un mecanismo de actualización automática.

Un despliegue reproducible también requiere los datos y modelos persistentes.
Una base recién creada usa `db/schema.sql`; la carga de datos y las imágenes médicas
se explican en `docs/DATASETS.md`. La recuperación de una instalación existente
se documenta en `docs/RESPALDO.md`. Un arranque vacío no reemplaza una restauración.

## Comprobación pública de lectura

URL de la entrega: <https://trazared.kogcake.com/>.
Swagger: <https://trazared.kogcake.com/api/docs>.

Con dependencias de API y `pruebas/requirements.txt` instaladas en un entorno Python,
y Chromium instalado por Playwright:

```sh
python -m playwright install chromium
python scripts/verificar_entrega_publica.py --navegador \
  --salida ../evidencia-trazared/entrega/publico.json
```

Solo hace GET anónimos. Comprueba las cinco dependencias de `/api/health` (un 200
degradado falla), rutas actuales en OpenAPI, protección de API/HAPI, MIME y SHA-256
de HTML/CSS/JS frente al clon local. En Chromium abre seis entradas, escritorio y móvil,
verifica controles y ausencia de desbordamiento horizontal, y guarda capturas.
Un hash distinto puede indicar despliegue atrasado o una respuesta corrupta: revisar
antes de presentarlo. No acredita igualdad del backend con HEAD; el informe lo declara.
Guarda revisión local y si existen cambios locales, sin copiar cuerpos clínicos ni secretos.

## Regresiones con datos efímeros

Requiere Docker, Python con las dependencias de API y pruebas, y Chromium:

```sh
bash scripts/probar_integracion.sh /RUTA/AL/ENTORNO/bin/python \
  ../evidencia-trazared/entrega
```

El script crea PostgreSQL y Mongo nuevos, sin volúmenes, con puertos aleatorios solo
en loopback. Verifica readiness, ejecuta las pruebas de integridad, perfil FHIR y
entregable, guarda `junit.xml` y evidencia, y elimina exclusivamente sus contenedores
al terminar. No carga credenciales de producción. FHIR, PACS y ML externos se aíslan
o simulan en las pruebas correspondientes; esto no reemplaza las comprobaciones públicas.

Los cinco escenarios de `tester.yaml` se ejecutan mediante el mismo código HTTP que usa
`scripts/verificar_tester.py`:

| Escenario | Resultado exigido e indicador |
|---|---|
| Fechas | Nacimiento futuro y remisiones antes del nacimiento/futuras dan 422; no aparece la ficha rechazada ni nuevas remisiones inválidas |
| Huérfanos | Ambas rutas de gestión devuelven 404 para una remisión inexistente; la lista Mongo no aumenta |
| Catálogo y transiciones | Institución desconocida y estado inicial inválido dan 422. Un salto de estado por PUT da 409 y queda una sola remisión pendiente |
| Semántica del socio | Un Bundle firmado con presiones incoherentes, UCUM incompatible o SNOMED fuera del perfil devuelve 422 y un OperationOutcome con la causa |
| Conciliación | Observation firmada de desconocido da 202; duplicado 409; documento distinto 422 y médico sin rol 403; identidad coincidente 200; repetición idempotente; pendiente desaparece y la Observation original queda en la historia |

Además, Chromium prueba **Interoperabilidad → Por conciliar → Conciliar paciente →
Confirmar coincidencia**, recarga la página y contrasta la historia por API. Las pruebas
existentes cubren las fechas en el formulario, radicación, gestión, cierre, sesión,
aprobación de IA/RAG y descargas. Los JSON de escenarios guardan únicamente pasos,
estados y códigos HTTP; las capturas locales contienen datos sintéticos.

## Tester completo del contrato

```sh
python scripts/verificar_tester.py --env-file RUTA_PRIVADA.env \
  --entorno-de-prueba --salida ../evidencia-trazared/entrega/tester-completo.json
```

Este comando escribe, crea un socio de bucle local, rota sus claves, cambia estados,
prueba bloqueos y reentrena modelos. Usarlo en la demostración autorizada, no como
monitor de disponibilidad. Los escenarios nuevos crean otro socio sintético sin URL
externa y sin rotar credenciales de socios existentes. La conciliación exige el secreto
federado correcto en la variable privada declarada por el contrato.

El informe diferencia `contrato_basico`, `flujo_integridad` y `manual_no_ejecutada`;
mantiene pendientes ante falta de credenciales o dependencias. Los totales por requisito
son conteos de casos, no un porcentaje automático de aprobación del proyecto.
Una excepción o HTTP incorrecto queda registrado como fallo. Nunca guardar tokens,
contraseñas ni cuerpos clínicos en el informe compartible.

## Acceso del docente y guion

- Confirmar que el docente puede leer el repositorio y abrir la URL antes de la sesión.
  Si el repositorio es privado, el dueño debe invitarlo con lectura; una URL de GitHub no demuestra ese permiso.
- Entregar las credenciales de demostración por canal privado, junto con la URL y los roles.
  No publicar claves en README, capturas o videos. El sistema no incorpora un rol nuevo de docente.
- Si el evaluador necesita YAML con secretos resueltos, usar `scripts/preparar_tester.py`
  como explica README; el archivo privado resultante no se añade a Git.
- Repetir la comprobación pública al iniciar y conservar su fecha. Verificar salud,
  login, un radicado sintético, gestión, cierre, descarga, validación por API y actualización
  de una segunda sesión. Mostrar la evidencia guardada de las pruebas que serían destructivas.
- Mantener separadas la evidencia API, las capturas SPA y los límites normativos/ML.
  Mostrar errores esperados además del recorrido válido.

Guion sugerido para 25 minutos: 3 de problema/arquitectura/roles, 7 de flujo SPA y API,
5 de IA/RAG y aprobación, 4 de seguridad y datos, 3 de interoperabilidad local y sus
límites, 3 de métricas/evidencia/preguntas. Ajustar responsables y tiempos con el equipo.
R33 no ocupa una demostración real hasta recibir nueva orden.

## Tareas humanas pendientes

Confirmar acceso de lectura del docente, entrega privada de cuentas, responsables y
ensayo cronometrado; asegurar equipo anfitrión, red y túnel durante la evaluación.
L05/R33 queda sin ejecutar ni agendar. Una caída posterior a la comprobación invalida
la disponibilidad en ese momento; el informe no promete disponibilidad futura.
