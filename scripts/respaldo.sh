#!/usr/bin/env bash
# Copia y restaura los datos de una instalación local de TrazaRed.
set -euo pipefail
umask 077

if [[ $# -ne 2 || ( "$1" != crear && "$1" != restaurar ) ]]; then
    echo "Uso: $0 crear|restaurar DIRECTORIO" >&2
    exit 2
fi

raiz=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
archivos=(trazared.dump hapi.dump mongo.archive orthanc.tar modelo.tar)
compose=(docker compose --project-directory "$raiz/docker" -f "$raiz/docker/docker-compose.yml")
if [[ -n "${TRAZARED_COMPOSE_OVERRIDE:-}" ]]; then
    compose+=(-f "$TRAZARED_COMPOSE_OVERRIDE")
fi

compose() { "${compose[@]}" "$@"; }
fallar() { echo "$*" >&2; exit 1; }

en_escritura=$(compose ps --status running -q api hapi-fhir orthanc ml)
if [[ -n "$en_escritura" ]]; then
    fallar "Detén api, hapi-fhir, orthanc y ml antes de copiar. Mantén Postgres y Mongo en ejecución."
fi

postgres() {
    local servicio=$1
    shift
    compose exec -T --user postgres "$servicio" "$@"
}
volumen() {
    local servicio=$1
    shift
    compose run --rm --no-deps -T --user 0 --entrypoint tar "$servicio" "$@"
}

if [[ "$1" == crear ]]; then
    destino=$(realpath -m -- "$2")
    [[ ! -e "$destino" ]] || fallar "El directorio de destino ya existe."
    provisional=$(mktemp -d -- "${destino}.XXXXXX")
    trap 'rm -rf -- "$provisional"' EXIT
    postgres db pg_dump -Fc -U trazared -d trazared > "$provisional/trazared.dump"
    postgres fhir-db pg_dump -Fc -U hapi -d hapi > "$provisional/hapi.dump"
    compose exec -T mongo mongodump --db trazared_huv --archive --gzip > "$provisional/mongo.archive"
    volumen orthanc -C /var/lib/orthanc/db -cpf - . > "$provisional/orthanc.tar"
    volumen ml -C /app/modelo -cpf - . > "$provisional/modelo.tar"
    (cd "$provisional" && sha256sum -- "${archivos[@]}" > SHA256SUMS)
    mv -T -- "$provisional" "$destino"
    trap - EXIT
    echo "Respaldo guardado en $destino"
    exit
fi

origen=$(realpath -- "$2")
(cd "$origen" && sha256sum --strict --check SHA256SUMS)
for archivo in "${archivos[@]}"; do
    [[ -s "$origen/$archivo" ]] || fallar "Falta el archivo $archivo."
    awk -v nombre="$archivo" '$2 == nombre {encontrado=1} END {exit !encontrado}' "$origen/SHA256SUMS"
done

# Solo destinos dedicados y recién inicializados. El esquema de TrazaRed ya
# existe por el init de Postgres, pero todavía no debe contener datos de la API.
[[ $(postgres db psql -XAt -v ON_ERROR_STOP=1 -U trazared -d trazared -c     "SELECT NOT (EXISTS (SELECT FROM pacientes) OR EXISTS (SELECT FROM usuarios) OR EXISTS (SELECT FROM remisiones) OR EXISTS (SELECT FROM auditoria));") == t ]]     || fallar "Postgres TrazaRed ya contiene datos."
[[ $(postgres fhir-db psql -XAt -v ON_ERROR_STOP=1 -U hapi -d hapi -c     "SELECT NOT EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public');") == t ]]     || fallar "La base de HAPI ya contiene tablas."
[[ $(compose exec -T mongo mongosh --quiet trazared_huv --eval 'db.getCollectionNames().length') == 0 ]]     || fallar "MongoDB ya contiene colecciones de TrazaRed."
archivos_pacs=$(volumen orthanc -C /var/lib/orthanc/db -cf - . | tar -tf - | sed '/^\.\/$/d')
[[ -z "$archivos_pacs" ]]     || fallar "El volumen de Orthanc ya contiene archivos."

postgres db pg_restore --single-transaction --clean --if-exists --exit-on-error -U trazared -d trazared < "$origen/trazared.dump"
postgres fhir-db pg_restore --single-transaction --exit-on-error -U hapi -d hapi < "$origen/hapi.dump"
compose exec -T mongo mongorestore --archive --gzip --stopOnError --nsInclude 'trazared_huv.*' < "$origen/mongo.archive"
volumen orthanc -C /var/lib/orthanc/db -xpf - < "$origen/orthanc.tar"
volumen ml -C /app/modelo -xpf - < "$origen/modelo.tar"
echo "Restauración terminada. Levanta los servicios después de recuperar la configuración y las llaves."
