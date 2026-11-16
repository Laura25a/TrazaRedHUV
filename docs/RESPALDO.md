# Respaldo y recuperación de TrazaRed

El respaldo contiene las dos bases PostgreSQL, la base documental de MongoDB, el volumen de imágenes e índice de Orthanc y el modelo entrenado. La copia incluye datos clínicos. Usa una ubicación privada y cifrada fuera del repositorio. Conserva por separado la configuración y las llaves originales, incluidas las versiones anteriores de cifrado, el índice ciego y la llave de auditoría. Sin ellas no se pueden leer o verificar los datos restaurados.

## Tomar una copia

Suspende las cargas directas y las integraciones entrantes durante la ventana de respaldo. Desde la raíz del repositorio:

```bash
docker compose -f docker/docker-compose.yml stop api hapi-fhir orthanc ml
scripts/respaldo.sh crear /ruta/privada/trazared-2026-11-16
docker compose -f docker/docker-compose.yml up -d
```

El script verifica que los servicios que escriben estén detenidos, exige que las tres bases estén disponibles y solo publica el directorio al completar la copia y su manifiesto SHA-256.

## Recuperar en una instalación vacía

Usa un entorno aislado con imágenes compatibles y volúmenes nuevos. El Compose del repositorio fija nombres y puertos. En el mismo equipo usa un archivo Compose de sobreescritura que defina nombres, puertos y proyecto propios. Si lo usas, pásalo tanto a Compose como al script mediante `TRAZARED_COMPOSE_OVERRIDE=/ruta/absoluta/compose.pruebas.yml`. Mantén la instalación de recuperación fuera del túnel hasta verificarla.

1. Instala la misma versión del proyecto y recupera su configuración privada y llaves desde la custodia separada.
2. Ejecuta `docker compose -f docker/docker-compose.yml up -d db fhir-db mongo` y espera a que respondan las bases. Prepara las imágenes de Orthanc y ML. Deja la API y HAPI apagados.
3. Ejecuta `scripts/respaldo.sh restaurar /ruta/privada/trazared-2026-11-16`. Si alguna fase falla, conserva el respaldo y comienza un nuevo intento con volúmenes vacíos. La recuperación completa abarca varios motores.
4. Levanta los servicios. Comprueba que se puede buscar un paciente autorizado por documento, abrir su historial y un adjunto DICOM previo, consultar la bitácora y una factura, verificar la cadena de auditoría y consultar por su identificador un recurso de HAPI. Comprueba también que ML carga el modelo recuperado.

En una instalación con PostgreSQL, MongoDB o PACS externos, toma las copias en esas instancias mediante sus herramientas administradas. Este script apunta a los servicios y volúmenes locales de Compose.
