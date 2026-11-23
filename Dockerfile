# docling-local — imagen solo CPU para convertir PDF/DOCX a Markdown sin red.
#
# Base fijada por digest (python:3.12-slim-bookworm, linux/amd64 multi-arch index).
# Para actualizarla: podman pull docker.io/library/python:3.12-slim-bookworm
#                    podman inspect --format '{{index .RepoDigests 0}}' docker.io/library/python:3.12-slim-bookworm
FROM docker.io/library/python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4

ARG DEBIAN_FRONTEND=noninteractive

# Paquetes del sistema en una sola capa:
#  - tesseract (+ spa, eng): OCR opcional (--ocr) vía TesseractCliOcrOptions.
#  - libreoffice-writer-nogui: conversión opcional .docx -> PDF (--docx-via-pdf).
#  - Fuentes con métricas compatibles con Arial/Times (Liberation), Calibri
#    (Carlito) y Cambria (Caladea); sin ellas LibreOffice redistribuye el texto.
# No hacen falta libgl1/libglib2.0-0: se usa opencv-python-headless.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-spa \
        tesseract-ocr-eng \
        libreoffice-writer-nogui \
        fonts-dejavu-core \
        fonts-liberation2 \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

COPY requirements.txt /tmp/requirements.txt

# 1) PyTorch solo CPU, primero y exclusivamente desde el índice CPU de PyTorch.
#    Se extraen los bloques torch/torchvision (pin +cpu con sus hashes) del
#    requirements.txt para no duplicar versiones ni hashes.
RUN awk '/^(torch|torchvision)==/{p=1} p{print} p && !/\\$/{p=0}' /tmp/requirements.txt > /tmp/req-torch.txt \
 && grep -q '^torch==.*+cpu' /tmp/req-torch.txt \
 && grep -q '^torchvision==.*+cpu' /tmp/req-torch.txt \
 && pip install --require-hashes --no-deps \
        --index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/req-torch.txt

# 2) Resto de dependencias desde PyPI, todas pineadas con hash. torch/torchvision
#    ya están instalados y satisfacen el pin, así que no se vuelven a buscar.
#    Se verifica que no se haya colado nada de CUDA ni OCR/VLM prohibidos.
RUN pip install --require-hashes \
        --index-url https://pypi.org/simple \
        -r /tmp/requirements.txt \
 && pip check \
 && ! pip list 2>/dev/null | grep -iE '^(nvidia-|triton |rapidocr|easyocr|onnxruntime|opencv-python )' \
 && rm -f /tmp/requirements.txt /tmp/req-torch.txt

# Usuario sin privilegios ni shell de login. /modelos pertenece a docling para
# que el volumen nombrado herede esa propiedad al crearse.
RUN groupadd --gid 1000 docling \
 && useradd --uid 1000 --gid 1000 --no-create-home --home-dir /tmp/home \
        --shell /usr/sbin/nologin docling \
 && install -d -o docling -g docling -m 0755 /modelos \
 && install -d -o root -g root -m 0755 /app

COPY --chmod=0644 convertir.py /app/convertir.py

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DOCLING_ARTIFACTS_PATH=/modelos \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/home

USER docling
WORKDIR /tmp

ENTRYPOINT ["python3", "/app/convertir.py"]
