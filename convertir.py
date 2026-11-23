#!/usr/bin/env python3
"""docling-local: convierte .pdf/.docx a Markdown con Docling, 100 % local.

Se ejecuta dentro del contenedor (ver convertir.sh). Para cada documento genera
<salida>/<ruta relativa sin extensión>/<nombre>.md con sus imágenes como PNG en
<nombre>_artifacts/, enlazadas por ruta relativa.

Códigos de salida: 0 sin fallos, 1 al menos un documento falló, 2 error de uso.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

EXTENSIONES = {".pdf", ".docx"}
TIMEOUT_SOFFICE_S = 120
MAX_MENSAJE = 300

log = logging.getLogger("convertir")


# --------------------------------------------------------------------------
# Configuración desde variables de entorno
# --------------------------------------------------------------------------
class ErrorUso(Exception):
    """Error de uso o de configuración: termina con código 2."""


def env_int(nombre: str, defecto: int, minimo: int = 1) -> int:
    valor = os.environ.get(nombre, "").strip() or str(defecto)
    try:
        n = int(valor)
    except ValueError:
        raise ErrorUso(f"{nombre}={valor!r} no es un entero") from None
    if n < minimo:
        raise ErrorUso(f"{nombre}={n} debe ser >= {minimo}")
    return n


def env_float(nombre: str, defecto: float, minimo: float) -> float:
    valor = os.environ.get(nombre, "").strip() or str(defecto)
    try:
        x = float(valor)
    except ValueError:
        raise ErrorUso(f"{nombre}={valor!r} no es un número") from None
    if x < minimo:
        raise ErrorUso(f"{nombre}={x} debe ser >= {minimo}")
    return x


@dataclass(frozen=True)
class Config:
    hilos: int
    escala_imagenes: float
    timeout_doc: float
    modelos: Path

    @classmethod
    def desde_entorno(cls) -> "Config":
        modelos = Path(os.environ.get("DOCLING_ARTIFACTS_PATH", "/modelos"))
        if not modelos.is_absolute():
            raise ErrorUso(
                f"DOCLING_ARTIFACTS_PATH={modelos} debe ser una ruta absoluta "
                "(con una relativa Docling busca los modelos en otro sitio y falla sin red)"
            )
        return cls(
            hilos=env_int("DOCLING_NUM_THREADS", 16),
            escala_imagenes=env_float("DOCLING_IMAGES_SCALE", 2.0, minimo=0.1),
            timeout_doc=env_float("DOCLING_TIMEOUT_DOC", 600.0, minimo=1.0),
            modelos=modelos,
        )


def verificar_modelos(modelos: Path) -> None:
    if not modelos.is_dir() or not any(modelos.glob("docling-project--*")):
        raise ErrorUso(
            f"modelos no encontrados en {modelos}. Ejecuta ./descargar-modelos.sh "
            "en el host (una sola vez, con red)."
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # mismo código 2 que argparse, mensaje en español
        self.print_usage(sys.stderr)
        self.exit(2, f"ERROR: {message}\n")


def parsear_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    p = Parser(
        prog="convertir.py",
        description="Convierte .pdf/.docx a Markdown con Docling, sin red.",
    )
    p.add_argument("entrada", nargs="?", default="/entrada",
                   help="archivo .pdf/.docx o directorio (recursivo). Por defecto /entrada")
    p.add_argument("--salida", default="/salida", help="directorio de salida (por defecto /salida)")
    p.add_argument("--ocr", action="store_true",
                   help="activa OCR con Tesseract (spa+eng) para PDF escaneados")
    p.add_argument("--forzar-ocr-pagina", action="store_true",
                   help="OCR de la página completa, ignorando la capa de texto (requiere --ocr)")
    p.add_argument("--docx-via-pdf", action="store_true",
                   help="convierte .docx a PDF con LibreOffice y aplica el pipeline PDF")
    p.add_argument("--backend-pdf", choices=["docling-parse", "pypdfium2"], default="docling-parse",
                   help="backend de PDF (por defecto docling-parse; pypdfium2 es más rápido "
                        "pero pierde precisión en tablas)")
    p.add_argument("--sobrescribir", action="store_true",
                   help="reprocesa aunque el destino ya exista")
    args = p.parse_args(argv)
    if args.forzar_ocr_pagina and not args.ocr:
        p.error("--forzar-ocr-pagina solo puede usarse junto con --ocr")
    return args


# --------------------------------------------------------------------------
# Descubrimiento de documentos
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Documento:
    ruta: Path          # ruta absoluta dentro del contenedor
    relativa: Path      # ruta relativa a la raíz de entrada (para mostrar y para la salida)

    @property
    def extension(self) -> str:
        return self.ruta.suffix.lower()

    @property
    def destino_relativo(self) -> Path:
        """<ruta relativa del original sin extensión>"""
        return self.relativa.with_suffix("")


def es_candidato(ruta: Path) -> bool:
    nombre = ruta.name
    # Archivos de bloqueo de Word/LibreOffice: no son documentos.
    if nombre.startswith("~$") or nombre.startswith(".~lock."):
        return False
    return ruta.suffix.lower() in EXTENSIONES


def descubrir(entrada: Path) -> tuple[Path, list[Documento]]:
    """Devuelve (raíz, documentos ordenados)."""
    if not entrada.exists():
        raise ErrorUso(f"no existe la ruta de entrada: {entrada}")
    if entrada.is_file():
        if not es_candidato(entrada):
            raise ErrorUso(f"{entrada} no es un .pdf ni un .docx")
        return entrada.parent, [Documento(entrada, Path(entrada.name))]
    if not entrada.is_dir():
        raise ErrorUso(f"{entrada} no es un archivo ni un directorio")

    docs: list[Documento] = []
    for dirpath, dirnames, filenames in os.walk(entrada, followlinks=False):
        dirnames.sort()
        for nombre in sorted(filenames):
            ruta = Path(dirpath) / nombre
            if es_candidato(ruta) and ruta.is_file():
                docs.append(Documento(ruta, ruta.relative_to(entrada)))
    if not docs:
        raise ErrorUso(f"no se encontraron archivos .pdf ni .docx en {entrada}")
    return entrada, docs


# --------------------------------------------------------------------------
# Docling
# --------------------------------------------------------------------------
def configurar_hilos(hilos: int) -> None:
    # Debe hacerse antes de importar torch (lo importa docling).
    os.environ.setdefault("OMP_NUM_THREADS", str(hilos))
    os.environ.setdefault("MKL_NUM_THREADS", os.environ["OMP_NUM_THREADS"])


def construir_conversor(cfg: Config, args: argparse.Namespace):
    from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
    from docling.backend.msword_backend import MsWordDocumentBackend
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.backend_options import MsWordBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        ConvertPipelineOptions,
        OcrMode,
        PdfPipelineOptions,
        TableFormerMode,
        TableStructureOptions,
        TesseractCliOcrOptions,
    )
    from docling.document_converter import DocumentConverter, PdfFormatOption, WordFormatOption

    try:  # barra "Loading weights" de transformers: ruido en stderr
        from transformers.utils import logging as tf_logging
        tf_logging.disable_progress_bar()
    except Exception:
        pass

    acelerador = AcceleratorOptions(num_threads=cfg.hilos, device=AcceleratorDevice.CPU)

    opciones_pdf = PdfPipelineOptions(
        artifacts_path=cfg.modelos,
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=acelerador,
        document_timeout=cfg.timeout_doc,
        # OCR solo con --ocr. --forzar-ocr-pagina equivale al antiguo
        # force_full_page_ocr=True (en Docling >= 2.1xx es OcrMode.FULL_PAGE).
        do_ocr=args.ocr,
        ocr_options=TesseractCliOcrOptions(
            lang=["spa", "eng"],
            mode=OcrMode.FULL_PAGE if args.forzar_ocr_pagina else OcrMode.DEFAULT,
        ),
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE, do_cell_matching=True
        ),
        generate_picture_images=True,
        images_scale=cfg.escala_imagenes,
        # Sin clasificación/descripción de figuras, fórmulas, código ni gráficos.
        do_picture_classification=False,
        do_picture_description=False,
        do_code_enrichment=False,
        do_formula_enrichment=False,
        do_chart_extraction=False,
    )
    backend_pdf = (
        PyPdfiumDocumentBackend if args.backend_pdf == "pypdfium2"
        else ThreadedDoclingParseDocumentBackend
    )

    opciones_word = ConvertPipelineOptions(
        artifacts_path=cfg.modelos,
        enable_remote_services=False,
        allow_external_plugins=False,
        accelerator_options=acelerador,
        document_timeout=cfg.timeout_doc,
        do_picture_classification=False,
        do_picture_description=False,
        do_chart_extraction=False,
    )

    return DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=opciones_pdf, backend=backend_pdf),
            InputFormat.DOCX: WordFormatOption(
                pipeline_options=opciones_word,
                backend=MsWordDocumentBackend,
                # Nunca seguir imágenes vinculadas (TargetMode="External"),
                # ni remotas ni locales.
                backend_options=MsWordBackendOptions(
                    enable_remote_fetch=False, enable_local_fetch=False
                ),
            ),
        },
    )


class FalloConversion(Exception):
    pass


def docx_a_pdf(docx: Path, tmp: Path) -> Path:
    """Convierte con LibreOffice headless. Lanza FalloConversion si no lo logra."""
    # Se trabaja sobre una copia: la entrada está montada en solo lectura y
    # LibreOffice intenta crear archivos de bloqueo junto al original.
    copia = tmp / ("doc" + docx.suffix.lower())
    shutil.copyfile(docx, copia)
    perfil = tmp / f"lo-perfil-{os.getpid()}"
    cmd = [
        "soffice",
        f"-env:UserInstallation=file://{perfil}",
        "--headless", "--norestore", "--nolockcheck",
        "--convert-to", "pdf", "--outdir", str(tmp), str(copia),
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    try:
        salida, _ = proc.communicate(timeout=TIMEOUT_SOFFICE_S)
    except subprocess.TimeoutExpired:
        # soffice lanza procesos hijos (soffice.bin): matar todo el grupo.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise FalloConversion(f"LibreOffice superó {TIMEOUT_SOFFICE_S} s") from None
    pdf = tmp / "doc.pdf"
    if proc.returncode != 0 or not pdf.is_file() or pdf.stat().st_size == 0:
        detalle = (salida or "").strip().splitlines()[-1:] or ["sin salida"]
        raise FalloConversion(f"LibreOffice código {proc.returncode}: {detalle[0]}")
    return pdf


def describir_errores(res) -> str:
    errores = getattr(res, "errors", None) or []
    if not errores:
        return f"estado {res.status.value}"
    partes = []
    for e in errores:
        modulo = (getattr(e, "module_name", "") or "").strip()
        mensaje = str(getattr(e, "error_message", e)).strip()
        texto = f"{modulo}: {mensaje}" if modulo else mensaje
        if texto not in partes:
            partes.append(texto)
    return "; ".join(partes)


def convertir_documento(conversor, doc: Documento, args, cfg: Config):
    """Devuelve el ConversionResult exitoso o lanza una excepción."""
    from docling.datamodel.base_models import ConversionStatus

    fuente = doc.ruta
    with tempfile.TemporaryDirectory(prefix="docx-pdf-", dir="/tmp") as tmp:
        if doc.extension == ".docx" and args.docx_via_pdf:
            try:
                fuente = docx_a_pdf(doc.ruta, Path(tmp))
            except (FalloConversion, OSError) as e:
                log.warning("ADVERTENCIA %s: --docx-via-pdf falló (%s); se usa el backend "
                            "nativo de Word", doc.relativa, e)
                fuente = doc.ruta

        inicio = time.monotonic()
        res = conversor.convert(fuente, raises_on_error=False)
        transcurrido = time.monotonic() - inicio

    if res.status == ConversionStatus.SUCCESS:
        return res
    if res.status == ConversionStatus.PARTIAL_SUCCESS and transcurrido >= cfg.timeout_doc:
        raise TimeoutError(f"superó DOCLING_TIMEOUT_DOC={cfg.timeout_doc:g} s")
    raise FalloConversion(describir_errores(res))


def paginas(res) -> int:
    n = len(getattr(res, "pages", None) or [])
    if n:
        return n
    try:
        return int(res.document.num_pages())
    except Exception:
        return 0


# --------------------------------------------------------------------------
# Escritura atómica
# --------------------------------------------------------------------------
def borrar(ruta: Path) -> None:
    if ruta.is_dir() and not ruta.is_symlink():
        shutil.rmtree(ruta, ignore_errors=True)
    elif ruta.exists() or ruta.is_symlink():
        try:
            ruta.unlink()
        except OSError:
            pass


def escribir_atomico(res, destino: Path, nombre: str) -> None:
    """Escribe <destino>/<nombre>.md (+ <nombre>_artifacts/) de forma atómica.

    Todo se genera en <destino>.tmp-<pid>-<rnd>/ y se renombra al final. Si algo
    falla, el temporal se borra y el destino previo (si lo había) queda intacto.
    """
    from docling_core.types.doc import ImageRefMode

    destino.parent.mkdir(parents=True, exist_ok=True)
    sufijo = f"{os.getpid()}-{secrets.token_hex(3)}"
    tmp = destino.with_name(f"{destino.name}.tmp-{sufijo}")
    viejo = destino.with_name(f"{destino.name}.tmp-{sufijo}-viejo")
    try:
        tmp.mkdir()
        res.document.save_as_markdown(
            tmp / f"{nombre}.md",
            # Relativo => docling lo resuelve junto al .md y escribe enlaces
            # relativos, que siguen siendo válidos tras el renombrado.
            artifacts_dir=Path(f"{nombre}_artifacts"),
            image_mode=ImageRefMode.REFERENCED,
        )
        artefactos = tmp / f"{nombre}_artifacts"
        if artefactos.is_dir() and not any(artefactos.iterdir()):
            artefactos.rmdir()  # sin imágenes: no dejar una carpeta vacía
        if destino.exists():
            destino.rename(viejo)
        try:
            tmp.rename(destino)
        except BaseException:
            if viejo.exists() and not destino.exists():
                viejo.rename(destino)
            raise
    finally:
        borrar(tmp)
        borrar(viejo)


# --------------------------------------------------------------------------
# Principal
# --------------------------------------------------------------------------
@dataclass
class Resumen:
    total: int = 0
    convertidos: int = 0
    omitidos: int = 0
    fallidos: list[tuple[str, str]] = field(default_factory=list)
    paginas: int = 0


def _sigterm(signum, frame):
    # podman stop envía SIGTERM: convertirlo en excepción para que los
    # bloques finally borren los temporales.
    raise KeyboardInterrupt(f"señal {signum}")


def configurar_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(message)s",
    )
    # Docling y sus dependencias son muy verbosas en INFO.
    for ruidoso in ("docling", "docling_core", "docling_parse", "docling_ibm_models",
                    "transformers", "huggingface_hub", "PIL", "torch", "rapidocr"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    configurar_logging()
    signal.signal(signal.SIGTERM, _sigterm)
    args = parsear_argumentos(argv)

    try:
        cfg = Config.desde_entorno()
        raiz, docs = descubrir(Path(args.entrada))
        salida = Path(args.salida)
        if not salida.is_dir():
            raise ErrorUso(f"el directorio de salida no existe: {salida}")
        if not os.access(salida, os.W_OK):
            raise ErrorUso(f"sin permiso de escritura en {salida} (revisa --userns=keep-id "
                           "y los permisos de la carpeta)")
        verificar_modelos(cfg.modelos)
    except ErrorUso as e:
        log.error("ERROR: %s", e)
        return 2

    # HOME vive en el tmpfs de /tmp: crearlo para cachés de fontconfig, etc.
    Path(os.environ.get("HOME", "/tmp/home")).mkdir(parents=True, exist_ok=True)
    configurar_hilos(cfg.hilos)

    resumen = Resumen(total=len(docs))
    inicio_total = time.monotonic()

    # Dos documentos con el mismo nombre base (p. ej. informe.pdf e
    # informe.docx) irían al mismo destino: solo se procesa el primero.
    vistos: dict[Path, Documento] = {}
    colisiones: dict[Documento, Documento] = {}
    for d in docs:
        clave = d.destino_relativo
        if clave in vistos:
            colisiones[d] = vistos[clave]
        else:
            vistos[clave] = d

    log.info("docling-local: %d documento(s) en %s -> %s (OCR: %s, backend PDF: %s, hilos: %d)",
             len(docs), raiz, salida, "sí" if args.ocr else "no", args.backend_pdf, cfg.hilos)

    conversor = None
    try:
        for i, doc in enumerate(docs, 1):
            prefijo = f"[{i}/{len(docs)}] {doc.relativa}"
            destino = salida / doc.destino_relativo

            if doc in colisiones:
                msg = (f"ConflictoDestino: mismo destino que {colisiones[doc].relativa} "
                       f"({doc.destino_relativo}/); renombra uno de los dos")
                log.error("ERROR %s: %s", doc.relativa, msg)
                resumen.fallidos.append((str(doc.relativa), msg))
                continue

            if destino.exists() and not args.sobrescribir:
                log.info("%s — omitido (ya existe %s; usa --sobrescribir)", prefijo,
                         destino.relative_to(salida))
                resumen.omitidos += 1
                continue

            t0 = time.monotonic()
            try:
                if conversor is None:
                    log.info("Cargando modelos desde %s ...", cfg.modelos)
                    conversor = construir_conversor(cfg, args)
                res = convertir_documento(conversor, doc, args, cfg)
                escribir_atomico(res, destino, doc.ruta.stem)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # cualquier fallo de un documento no corta el lote
                tipo = type(e).__name__
                mensaje = str(e).strip().splitlines()[0] if str(e).strip() else repr(e)
                if len(mensaje) > MAX_MENSAJE:
                    mensaje = mensaje[: MAX_MENSAJE - 1] + "…"
                log.error("ERROR %s: %s: %s", doc.relativa, tipo, mensaje)
                log.debug("traza", exc_info=True)
                resumen.fallidos.append((str(doc.relativa), f"{tipo}: {mensaje}"))
                continue

            n = paginas(res)
            resumen.paginas += n
            resumen.convertidos += 1
            # El backend nativo de Word no pagina: no hay número de páginas.
            detalle = f"{n} pág." if n else "sin paginar"
            log.info("%s — OK (%s, %.1f s)", prefijo, detalle, time.monotonic() - t0)
    except KeyboardInterrupt:
        log.error("Interrumpido: se detiene el lote (los temporales se han borrado).")
        imprimir_resumen(resumen, time.monotonic() - inicio_total)
        return 1

    imprimir_resumen(resumen, time.monotonic() - inicio_total)
    return 1 if resumen.fallidos else 0


def imprimir_resumen(r: Resumen, segundos: float) -> None:
    lineas = [
        "",
        "Resumen",
        f"  Total:        {r.total}",
        f"  Convertidos:  {r.convertidos}",
        f"  Omitidos:     {r.omitidos}",
        f"  Fallidos:     {len(r.fallidos)}",
    ]
    lineas += [f"    - {ruta}: {motivo}" for ruta, motivo in r.fallidos]
    lineas += [
        f"  Páginas:      {r.paginas}",
        f"  Tiempo:       {segundos:.1f} s",
    ]
    print("\n".join(lineas), flush=True)


if __name__ == "__main__":
    sys.exit(main())
