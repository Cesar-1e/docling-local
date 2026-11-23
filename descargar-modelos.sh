#!/usr/bin/env bash
# Descarga (una sola vez) los modelos del pipeline estándar de Docling al
# volumen nombrado 'docling-modelos'. Es el ÚNICO paso que usa red.
#
# Modelos: layout (docling-layout-heron) y tableformer (TableFormer v1,
# usado en modo ACCURATE). Idempotente: si ya están, no descarga nada.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
VOLUMEN_MODELOS="docling-modelos"
MODELOS=(layout tableformer)
# Subdirectorios que deben existir tras la descarga (repo_id con '/' -> '--').
ESPERADOS=(docling-project--docling-layout-heron docling-project--docling-models)

leer_env() {
    local clave="$1" defecto="$2" valor=""
    if [[ -f "$ENV_FILE" ]]; then
        valor="$(grep -E "^[[:space:]]*${clave}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '[:space:]"'"'" || true)"
    fi
    printf '%s' "${valor:-$defecto}"
}
IMAGEN="${IMAGEN:-$(leer_env IMAGEN docling-local:0.1.0)}"

if ! podman image exists "$IMAGEN"; then
    echo "ERROR: no existe la imagen $IMAGEN. Constrúyela con:" >&2
    echo "  podman build -t $IMAGEN ." >&2
    exit 2
fi

if ! podman volume exists "$VOLUMEN_MODELOS"; then
    echo "Creando volumen $VOLUMEN_MODELOS ..."
    podman volume create "$VOLUMEN_MODELOS" >/dev/null
fi

# Opciones comunes: mismo mapeo de usuario que convertir.sh, para que los
# archivos del volumen pertenezcan al usuario 'docling' (UID 1000) de la imagen.
comunes=(
    --rm
    --userns=keep-id:uid=1000,gid=1000
    --security-opt label=disable
    --read-only
    --tmpfs /tmp:rw,size=2g
    --cap-drop=ALL
    --security-opt no-new-privileges
)

script_verificar='
faltan=0
for d in "$@"; do
    if [ -d "/modelos/$d" ] && [ -n "$(ls -A "/modelos/$d" 2>/dev/null)" ]; then
        echo "  ok      $d"
    else
        echo "  falta   $d"; faltan=1
    fi
done
exit $faltan'

verificar() {
    podman run "${comunes[@]}" --network none \
        -v "${VOLUMEN_MODELOS}:/modelos:ro" \
        --entrypoint sh "$IMAGEN" -c "$script_verificar" verificar "${ESPERADOS[@]}"
}

echo "Comprobando modelos en el volumen $VOLUMEN_MODELOS ..."
if verificar; then
    echo "Los modelos ya están descargados; no se descarga nada."
else
    echo "Descargando modelos (${MODELOS[*]}) — único paso con red ..."
    podman run "${comunes[@]}" \
        -e HF_HUB_OFFLINE=0 \
        -e TRANSFORMERS_OFFLINE=0 \
        -v "${VOLUMEN_MODELOS}:/modelos:rw" \
        --entrypoint docling-tools "$IMAGEN" \
        models download "${MODELOS[@]}" -o /modelos

    echo "Verificando ..."
    if ! verificar; then
        echo "ERROR: la descarga terminó pero faltan modelos en el volumen." >&2
        exit 1
    fi
fi

echo "Contenido de /modelos:"
podman run "${comunes[@]}" --network none \
    -v "${VOLUMEN_MODELOS}:/modelos:ro" \
    --entrypoint sh "$IMAGEN" -c 'ls -1d /modelos/docling-project--* && du -sh /modelos'
