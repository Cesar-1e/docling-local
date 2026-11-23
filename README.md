# docling-local

Convierte archivos `.pdf` y `.docx` (uno suelto o todos los de un directorio) a
Markdown con [Docling](https://github.com/docling-project/docling), **100 % local
y sin red**. Cada documento produce un `.md` con sus imágenes exportadas como PNG
externos enlazados por ruta relativa.

**Flujo:** `podman build` (una vez) → `./descargar-modelos.sh` (una vez, con red)
→ `./convertir.sh <archivo|directorio>` (cada uso, contenedor efímero con
`--network none`).

## Requisitos del host

- Bluefin / Fedora Atomic (o cualquier Linux) con **Podman rootless**.
- Unos **5 GB libres**: imagen ≈ 2,2 GB + modelos ≈ 0,7 GB + margen de build.
- **No requiere GPU.** No se instala nada en el host.

## Uso

### 1. Construir la imagen

```bash
podman build -t docling-local:0.1.0 .
podman image ls docling-local      # ≈ 2,2 GB; si supera 5 GB entró torch con CUDA
```

### 2. Descargar los modelos (una vez)

```bash
./descargar-modelos.sh
```

Es el **único** paso con red. Baja `layout` (docling-layout-heron) y `tableformer`
al volumen nombrado `docling-modelos` (≈ 670 MB). Es idempotente: si los modelos
ya están, no descarga nada.

### 3. Convertir

```bash
./convertir.sh informe.pdf                       # un archivo
./convertir.sh ~/Documentos/contratos/            # un directorio, recursivo
./convertir.sh escaneo.pdf --ocr                  # PDF escaneado (sin capa de texto)
./convertir.sh escaneo.pdf --ocr --forzar-ocr-pagina   # capa de texto basura
./convertir.sh memoria.docx --docx-via-pdf        # .docx con maquetación compleja
./convertir.sh carpeta/ --salida ~/md --sobrescribir
```

La salida por defecto es `./salida` (en el directorio desde el que se ejecuta).
Estructura generada:

```
salida/
└── <ruta relativa del original sin extensión>/
    ├── <nombre>.md
    └── <nombre>_artifacts/        # solo si el documento tiene imágenes
        └── image_000000_<hash>.png
```

Al procesar un directorio se replica su estructura. Al final se imprime un
resumen (total, convertidos, omitidos, fallidos con su motivo, páginas y tiempo).

**Códigos de salida:** `0` sin fallos · `1` falló al menos un documento ·
`2` error de uso (ruta inexistente, sin documentos válidos, opciones
incompatibles, imagen o modelos ausentes).

### Opciones y cuándo usarlas

| Opción | Cuándo usarla |
|---|---|
| `--salida DIR` | Para escribir en otra carpeta del host (se crea si no existe). |
| `--ocr` | PDF escaneados o con partes rasterizadas. Usa Tesseract (`spa`+`eng`). Más lento; sin él, un escaneado sale casi vacío. |
| `--forzar-ocr-pagina` | Solo con `--ocr`. OCR de la página completa ignorando la capa de texto del PDF; útil si esa capa está corrupta o mal codificada. En Docling ≥ 2.1xx equivale a `OcrMode.FULL_PAGE` (sustituye al antiguo `force_full_page_ocr=True`). |
| `--docx-via-pdf` | `.docx` con cuadros de texto, columnas o maquetación que el backend nativo pierde. Convierte con LibreOffice y aplica el pipeline PDF (layout + TableFormer). Si LibreOffice falla, cae al backend nativo y lo avisa. Para extraer el texto **dentro de imágenes** (capturas) hay que añadir también `--ocr`. Las listas anidadas se aplanan por esta vía. |
| `--backend-pdf pypdfium2` | Solo para casos puntuales (PDF enormes o con timeout): 2–3 veces más rápido, peor en tablas densas. |
| `--sobrescribir` | Reprocesar documentos ya convertidos. Sin ella, los existentes se omiten. |

## Variables de entorno

Copia `.env.example` a `.env` y ajusta. `convertir.sh` lo pasa al contenedor con
`--env-file` y lee de él `IMAGEN` y `DOCLING_CPUS`.

| Variable | Default | Descripción |
|---|---|---|
| `DOCLING_NUM_THREADS` | `16` | Hilos de inferencia. 16 aprovecha los núcleos P con hyperthreading; más de 20 no mejora y compite con el escritorio. |
| `OMP_NUM_THREADS` | `16` | Debe ser igual a `DOCLING_NUM_THREADS`. |
| `DOCLING_CPUS` | `16` | Límite de CPU del contenedor (`--cpus`). |
| `DOCLING_IMAGES_SCALE` | `2.0` | Escala de las imágenes exportadas (2.0 ≈ 144 DPI). 1.0 es más rápido pero da PNG borrosos. |
| `DOCLING_TIMEOUT_DOC` | `600` | Segundos máximos por documento antes de marcarlo como fallido. |
| `DOCLING_ARTIFACTS_PATH` | `/modelos` | **No cambiar.** Ruta absoluta de los modelos; con una relativa, Docling los busca en otro sitio y falla sin red. |
| `HF_HUB_OFFLINE` | `1` | **No cambiar** en conversión. Impide cualquier intento de descarga. |
| `IMAGEN` | `docling-local:0.1.0` | Tag de imagen que usan los scripts. |

## Seguridad y aislamiento

La conversión corre con: `--network none`, `--read-only`, `/tmp` en tmpfs de
4 GB, `--memory 16g`, `--cap-drop=ALL`, `no-new-privileges`, usuario sin
privilegios `docling` (UID 1000), entrada y modelos montados en solo lectura.
Las imágenes vinculadas externamente en `.docx` (`TargetMode="External"`) nunca
se descargan ni se leen del disco (`enable_remote_fetch` y `enable_local_fetch`
desactivados): quedan como `<!-- image -->`.

Comprobación del aislamiento:

```bash
podman run --rm --network none --entrypoint python3 docling-local:0.1.0 -c \
  "import urllib.request; urllib.request.urlopen('https://huggingface.co', timeout=5)"
# -> URLError: Temporary failure in name resolution
```

## Notas técnicas

**Por qué solo CPU.** El uso es esporádico y con CPU se procesan ≈ 1–1,5 pág/s
sin OCR. La GPU (RTX 5060 Ti, Blackwell sm_120) obligaría a usar wheels de
PyTorch `cu128`, multiplicaría por 3–4 el tamaño de la imagen y competiría por
VRAM con otros servicios. PyTorch se instala desde el índice CPU
(`download.pytorch.org/whl/cpu`, versión `+cpu`); el wheel de PyPI arrastra
≈ 4 GB de CUDA inútiles. El build falla si se cuela cualquier paquete `nvidia-*`.

**Por qué `label=disable` y no `:z`/`:Z`.** La entrada puede ser cualquier carpeta
del usuario (incluso `$HOME`); `:z` la reetiquetaría con SELinux de forma
permanente y afectaría a otros programas. Desactivar el etiquetado solo para este
contenedor, que además no tiene red ni capacidades, es el menor riesgo.

**Por qué `--userns=keep-id:uid=1000,gid=1000`.** Mapea tu usuario del host al
usuario `docling` (UID 1000) de la imagen, así los archivos de `salida/`
quedan a tu nombre aunque tu UID en el host no sea 1000.

**Por qué `ImageRefMode.REFERENCED`.** Por defecto Docling incrusta las imágenes
en Base64 (Markdown ilegible y enorme) y `export_to_markdown` sin materializar
las imágenes deja marcadores vacíos. Con `save_as_markdown(...,
image_mode=REFERENCED)` y un `artifacts_dir` relativo, las imágenes se guardan
como PNG y los enlaces son relativos al `.md`.

**Por qué `docling-parse`.** Es el backend PDF más preciso para tablas;
`pypdfium2` es 2–3 veces más rápido pero pierde precisión en tablas densas.

**Por qué `docling-slim` y no `docling`.** Desde la serie 2.9x, el paquete
`docling` es un meta-paquete equivalente a `docling-slim[standard]`, que arrastra
**RapidOCR** (prohibido aquí), `docling-core[chunking]` y backends de
pptx/xlsx/email. Se instala `docling-slim` con solo los extras necesarios
(`format-pdf`, `format-docx`, `convert-core`, `models-local`, `cli`). Es el mismo
código y la misma numeración de versiones; los avisos de seguridad de GitHub se
publican para ambos nombres. Tampoco se instalan EasyOCR, VLM ni
`opencv-python` (se usa `opencv-python-headless`).

**Escritura atómica.** Cada documento se genera en `<destino>.tmp-<pid>-<rnd>/` y
se renombra al terminar; si falla, se borra. Con `--sobrescribir`, el destino
anterior solo se elimina después de que el nuevo esté completo. `SIGTERM`
(`podman stop`) también limpia los temporales. Si el contenedor muere por
`SIGKILL`/OOM pueden quedar restos; se limpian con
`find salida -name '*.tmp-*' -prune -exec rm -rf {} +`.

**Nombres que colisionan.** `informe.pdf` e `informe.docx` en la misma carpeta irían
al mismo destino (`informe/`): se procesa el primero en orden alfabético y el
otro se marca como fallido (`ConflictoDestino`) para que lo renombres.

**Timeout.** Si un documento supera `DOCLING_TIMEOUT_DOC`, se marca como fallido
y no se escribe un Markdown parcial.

### Actualizar Docling

1. Consultar la última versión estable en PyPI (`docling-slim`, `docling-core`) y
   los avisos en el [GitHub Advisory Database](https://github.com/advisories?query=ecosystem%3Apip+docling).
   Si hace falta, subir los mínimos de `requirements.in`.
2. Re-pinear con hashes (dentro de un contenedor, sin instalar nada en el host):

   ```bash
   podman run --rm --security-opt label=disable -v "$PWD":/w -w /w \
     docker.io/library/python:3.12-slim-bookworm sh -c '
       pip install -q uv &&
       uv pip compile requirements.in --generate-hashes --torch-backend cpu \
         --python-version 3.12 --python-platform x86_64-manylinux_2_36 \
         --emit-index-annotation -o requirements.txt'
   ```

   Revisar el diff: torch/torchvision deben seguir con sufijo `+cpu` y no debe
   aparecer `rapidocr`, `easyocr`, `onnxruntime`, `opencv-python` (no headless)
   ni `nvidia-*`. Opcional: auditar con `pip-audit -r requirements.txt`.
3. Si se actualiza la base, obtener el nuevo digest
   (`podman pull` + `podman inspect --format '{{index .RepoDigests 0}}'`) y
   cambiarlo en el `FROM` del `Dockerfile`.
4. Subir el tag (`IMAGEN` en `.env`) y reconstruir: `podman build -t docling-local:<nueva> .`
5. Volver a descargar los modelos por si cambiaron los pesos o sus nombres:
   `podman volume rm docling-modelos && ./descargar-modelos.sh`
6. Repetir el plan de verificación (`pruebas/README.md`).

## Limitaciones conocidas

- Las tablas de PDF que cruzan saltos de página pueden salir fragmentadas en
  varias tablas Markdown o con encabezados duplicados.
- Las tablas con muchas celdas combinadas pueden requerir ajuste manual.
- El backend nativo de `.docx` no hace OCR de las imágenes: el texto dentro de
  capturas solo se obtiene con `--docx-via-pdf --ocr`.
- Por la vía `--docx-via-pdf` las listas anidadas se aplanan y las imágenes que
  el modelo de layout interpreta como texto (p. ej. capturas con fondo blanco) no
  se exportan como PNG.
- El backend nativo de Word no pagina: el resumen no cuenta páginas de `.docx`.
- PDF cifrados con contraseña: fallan (se registran como fallidos).

## Qué hacer cuando falla lo típico

1. **"modelos no encontrados" o intento de conexión a Hugging Face** → ejecutar
   `./descargar-modelos.sh`. Comprobar que `DOCLING_ARTIFACTS_PATH=/modelos` y
   `HF_HUB_OFFLINE=1` no se hayan cambiado en `.env`.
2. **`Permission denied` en la salida** → revisar que `convertir.sh` use
   `--userns=keep-id` y que tu usuario pueda escribir en la carpeta de salida
   (`ls -ld salida`). Si la carpeta la creó otro contenedor con otro mapeo de
   UID, corrígelo con `podman unshare chown -R 0:0 salida`.
3. **Timeout en un PDF grande** → subir `DOCLING_TIMEOUT_DOC` en `.env`, o en ese
   caso puntual usar `--backend-pdf pypdfium2`.

## Plan de verificación

Ver la sección 8 de la especificación original; los documentos de prueba se
describen en [`pruebas/README.md`](pruebas/README.md). Resumen:

```bash
podman info --format '{{.Host.Security.Rootless}}'                # true
podman build -t docling-local:0.1.0 . && podman image ls docling-local
podman run --rm --entrypoint python3 docling-local:0.1.0 -c \
  "import torch, importlib.metadata as m; print(torch.__version__, torch.cuda.is_available(), m.version('docling-slim'), m.version('docling-core'))"
./descargar-modelos.sh                                             # 2.ª vez: no descarga
./convertir.sh pruebas/tabla-multipagina.pdf
./convertir.sh pruebas/                                            # fallidos = 1, código 1
grep -c base64 salida/*/*.md                                       # todo 0
./convertir.sh pruebas/escaneado.pdf --ocr --sobrescribir
./convertir.sh pruebas/con-imagen.docx --docx-via-pdf --ocr --sobrescribir
./convertir.sh pruebas/imagen-externa.docx
find salida -name '*.tmp-*'                                        # vacío
ls -ln salida
```
