#!/usr/bin/env bash
# Convierte un .pdf/.docx (o todos los de un directorio) a Markdown con Docling,
# en un contenedor efímero SIN RED.
#
# Uso: ./convertir.sh <archivo|directorio> [--salida DIR] [opciones de convertir.py]
#   Opciones: --ocr  --forzar-ocr-pagina  --docx-via-pdf
#             --backend-pdf {docling-parse,pypdfium2}  --sobrescribir
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
VOLUMEN_MODELOS="docling-modelos"

uso() {
    cat >&2 <<'EOF'
Uso: ./convertir.sh <archivo|directorio> [--salida DIR] [opciones]

  <archivo|directorio>   .pdf/.docx, o directorio (recursivo) con ellos.
  --salida DIR           Carpeta de salida en el host (por defecto ./salida).
  --ocr                  OCR con Tesseract (spa+eng) para PDF escaneados.
  --forzar-ocr-pagina    OCR de la página completa (solo junto con --ocr).
  --docx-via-pdf         Convierte .docx a PDF con LibreOffice y usa el pipeline PDF.
  --backend-pdf B        docling-parse (por defecto) o pypdfium2.
  --sobrescribir         Reprocesa aunque el destino ya exista.

Códigos de salida: 0 sin fallos, 1 algún documento falló, 2 error de uso.
EOF
}

# Lee una variable de .env sin ejecutarlo (solo líneas KEY=VALOR).
leer_env() {
    local clave="$1" defecto="$2" valor=""
    if [[ -f "$ENV_FILE" ]]; then
        valor="$(grep -E "^[[:space:]]*${clave}=" "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '[:space:]"'"'" || true)"
    fi
    printf '%s' "${valor:-$defecto}"
}

IMAGEN="${IMAGEN:-$(leer_env IMAGEN docling-local:0.1.0)}"
DOCLING_CPUS="${DOCLING_CPUS:-$(leer_env DOCLING_CPUS 16)}"

# --- Argumentos -------------------------------------------------------------
entrada=""
salida=""
pasar=()
while (($#)); do
    case "$1" in
        -h|--help) uso; exit 0 ;;
        --salida)
            [[ $# -ge 2 ]] || { echo "ERROR: --salida requiere un directorio" >&2; exit 2; }
            salida="$2"; shift 2 ;;
        --salida=*) salida="${1#--salida=}"; shift ;;
        --backend-pdf)
            [[ $# -ge 2 ]] || { echo "ERROR: --backend-pdf requiere un valor" >&2; exit 2; }
            pasar+=("$1" "$2"); shift 2 ;;
        -*) pasar+=("$1"); shift ;;
        *)
            if [[ -z "$entrada" ]]; then entrada="$1"
            else echo "ERROR: argumento inesperado: $1" >&2; uso; exit 2
            fi
            shift ;;
    esac
done

if [[ -z "$entrada" ]]; then
    uso; exit 2
fi

if ! entrada_abs="$(realpath -e -- "$entrada" 2>/dev/null)"; then
    echo "ERROR: no existe la ruta de entrada: $entrada" >&2
    exit 2
fi

if [[ -f "$entrada_abs" ]]; then
    montaje_entrada="$(dirname -- "$entrada_abs")"
    ruta_contenedor="/entrada/$(basename -- "$entrada_abs")"
elif [[ -d "$entrada_abs" ]]; then
    montaje_entrada="$entrada_abs"
    ruta_contenedor="/entrada"
else
    echo "ERROR: la entrada no es un archivo ni un directorio: $entrada_abs" >&2
    exit 2
fi

salida="${salida:-$PWD/salida}"
mkdir -p -- "$salida"
salida_abs="$(realpath -e -- "$salida")"

# Los dos puntos rompen la sintaxis de -v origen:destino:opciones.
for ruta in "$montaje_entrada" "$salida_abs"; do
    if [[ "$ruta" == *:* ]]; then
        echo "ERROR: la ruta contiene ':' y no se puede montar: $ruta" >&2
        exit 2
    fi
done

# --- Comprobaciones previas -------------------------------------------------
if ! podman image exists "$IMAGEN"; then
    echo "ERROR: no existe la imagen $IMAGEN. Constrúyela con:" >&2
    echo "  podman build -t $IMAGEN ." >&2
    exit 2
fi

# Importante: comprobar antes de 'podman run -v', que crearía un volumen vacío.
if ! podman volume exists "$VOLUMEN_MODELOS" \
   || ! podman run --rm --network none --userns=keep-id:uid=1000,gid=1000 \
            --security-opt label=disable --read-only --cap-drop=ALL \
            --security-opt no-new-privileges \
            -v "${VOLUMEN_MODELOS}:/modelos:ro" --entrypoint sh "$IMAGEN" \
            -c 'ls -d /modelos/docling-project--* >/dev/null 2>&1'; then
    echo "ERROR: el volumen '$VOLUMEN_MODELOS' no existe o no contiene modelos." >&2
    echo "  Ejecuta primero: ./descargar-modelos.sh" >&2
    exit 2
fi

env_args=()
if [[ -f "$ENV_FILE" ]]; then
    env_args=(--env-file "$ENV_FILE")
fi

# --- Conversión (sin red) ---------------------------------------------------
# label=disable en vez de :z/:Z: la entrada puede ser cualquier carpeta del
# usuario y :z la reetiquetaría con SELinux de forma permanente.
# keep-id:uid=1000 mapea el usuario del host al usuario 'docling' (UID 1000)
# de la imagen, así la salida queda a nombre del usuario del host.
exec podman run --rm \
    --network none \
    --userns=keep-id:uid=1000,gid=1000 \
    --security-opt label=disable \
    --read-only \
    --tmpfs /tmp:rw,size=4g \
    --memory 16g \
    --cpus "$DOCLING_CPUS" \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    -v "${montaje_entrada}:/entrada:ro" \
    -v "${salida_abs}:/salida:rw" \
    -v "${VOLUMEN_MODELOS}:/modelos:ro" \
    "${env_args[@]}" \
    "$IMAGEN" \
    "$ruta_contenedor" --salida /salida "${pasar[@]}"
